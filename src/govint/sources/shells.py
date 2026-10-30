"""Shell-company clustering: who else is registered at the same address, and who shares officers.

Pulls addresses for a name from the FAA registry, ICIJ Offshore Leaks and SEC EDGAR, then lists
everything else on file at each one. An address with dozens of unrelated registrants is usually a
registered agent, trust company or mail drop, which is where shell companies live."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .. import names
from ..http import SourceError
from . import edgar, faa, offshore
from .base import Ctx

MASS = 10  # distinct registrants at one address before we call it a mass-registration address
_ZIP = re.compile(r"\b(\d{5})(?:-?\d{4})?\b")


@dataclass
class Address:
    text: str
    found_via: list[str] = field(default_factory=list)
    faa: list[dict] = field(default_factory=list)  # aircraft registrations here
    icij: list[dict] = field(default_factory=list)  # offshore entities/officers here

    @property
    def registrants(self) -> list[str]:
        seen: dict[str, str] = {}
        for r in self.faa:
            seen.setdefault(names.normalize(r["name"]), r["name"])
        for r in self.icij:
            seen.setdefault(names.normalize(r["name"]), r["name"])
        return list(seen.values())

    @property
    def mass(self) -> bool:
        return len(self.registrants) >= MASS


@dataclass
class Report:
    query: str
    addresses: list[Address] = field(default_factory=list)
    shared_officers: list[dict] = field(default_factory=list)  # ICIJ: officers of the subject and their other entities
    notes: list[str] = field(default_factory=list)


def _street_key(street: str) -> list[str]:
    return names.address_key(street).split()


def _faa_addr_text(r: dict) -> str:
    street = " ".join(x for x in (r.get("street"), r.get("street2")) if x and x.strip())
    return f"{street}, {r.get('city', '').strip()}, {r.get('state', '')} {(r.get('zip') or '')[:5]}".strip()


def faa_at(ctx: Ctx, text: str, limit: int = 5000) -> list[dict]:
    """FAA registrations at a street address. Needs a ZIP or a house number in `text`."""
    if not faa.available(ctx):
        return []
    key = set(names.address_key(text).split())
    zips = [m.group(1) for m in _ZIP.finditer(text)]
    number = re.match(r"\s*(\d+)\b", text)
    db = ctx.db()
    if zips and zips[-1] != (number.group(1) if number else None):
        rows = db.execute("select * from faa_master where substr(zip, 1, 5) = ?", (zips[-1],)).fetchall()
    elif number:
        rows = db.execute("select * from faa_master where street like ?", (f"{number.group(1)} %",)).fetchall()
    else:
        return []
    out = []
    for r in rows:
        sk = _street_key(" ".join(x for x in (r["street"], r["street2"]) if x))
        if sk and set(sk) <= key and (not number or sk[0] == number.group(1)):
            out.append(dict(r))
    return out[:limit]


def top_faa(ctx: Ctx, state: str | None = None, limit: int = 25) -> list[dict]:
    """Addresses with the most distinct aircraft owners: trust companies, agents and mail drops."""
    q = "select name, street, street2, city, state, zip, n_number from faa_master"
    args: tuple = ()
    if state:
        q += " where state = ?"
        args = (state.upper(),)
    groups: dict[tuple, dict] = defaultdict(lambda: {"owners": set(), "aircraft": 0, "sample": []})
    for r in ctx.db().execute(q, args):
        street = " ".join(x for x in (r["street"], r["street2"]) if x and x.strip())
        if not street.strip():
            continue
        k = (names.address_key(street), (r["zip"] or "")[:5])
        g = groups[k]
        g["aircraft"] += 1
        norm = names.normalize(r["name"])
        if norm not in g["owners"]:
            g["owners"].add(norm)
            if len(g["sample"]) < 4:
                g["sample"].append(r["name"].strip())
        g.setdefault("text", _faa_addr_text(dict(r)))
    ranked = sorted(groups.values(), key=lambda g: -len(g["owners"]))[:limit]
    return [{"address": g["text"], "owners": len(g["owners"]), "aircraft": g["aircraft"], "sample": g["sample"]}
            for g in ranked]


def _looks_like_address(q: str) -> bool:
    return bool(re.match(r"\s*\d+\s+\w", q))


def report(ctx: Ctx, query: str) -> Report:
    rep = Report(query)
    found: dict[str, Address] = {}

    def add(text: str, via: str) -> None:
        text = re.sub(r"\s+", " ", text).strip(" ,")
        if not text:
            return
        k = names.address_key(text)
        a = found.setdefault(k, Address(text))
        if via not in a.found_via:
            a.found_via.append(via)

    if _looks_like_address(query):
        add(query, "query")
    else:
        if faa.available(ctx):
            for r in faa.by_owner(ctx, query):
                add(_faa_addr_text(r), f"FAA {r['n_number']}")
        if offshore.available(ctx):
            for h in offshore.search(ctx, query, threshold=95, limit=8):
                for ln in offshore.links(ctx, h.node["id"]):
                    if ln["rel"] == "registered_address":
                        add(ln["name"], f"ICIJ {h.node['source']}")
                    elif ln["rel"] == "officer_of" and ln["dir"] == "in":
                        others = [x for x in offshore.links(ctx, ln["id"]) if x["rel"] == "officer_of"
                                  and x["dir"] == "out" and x["id"] != h.node["id"]]
                        if others:
                            rep.shared_officers.append({"officer": ln["name"], "role": ln["link"], "of": h.node["name"],
                                                        "also": [o["name"] for o in others[:15]],
                                                        "also_count": len(others), "url": offshore.NODE_URL.format(id=ln["id"])})
        try:
            hits = edgar.search(ctx, query)
            if hits and hits[0]["score"] >= 90:
                sub = edgar.submissions(ctx, hits[0]["cik"])
                for kind, a in (sub.get("addresses") or {}).items():
                    if t := edgar._addr_text(a):
                        add(t, f"SEC {kind} address")
        except SourceError as e:
            rep.notes.append(f"SEC: {e}")
    if not found:
        rep.notes.append("No addresses on file for that name in FAA, ICIJ or SEC.")
    for a in found.values():
        a.faa = faa_at(ctx, a.text)
        if offshore.available(ctx):
            a.icij = offshore.at_address(ctx, a.text)
    rep.addresses = sorted(found.values(), key=lambda a: -len(a.registrants))
    if not faa.available(ctx):
        rep.notes.append("FAA registry not synced (`govint sync faa`).")
    if not offshore.available(ctx):
        rep.notes.append("ICIJ Offshore Leaks not synced (`govint sync offshore`).")
    return rep

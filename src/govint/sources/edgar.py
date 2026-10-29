"""SEC EDGAR: company lookup, profile, and insiders from Forms 3/4/5."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field

from .. import names
from ..graph import ADDRESS, COMPANY, PERSON, Edge, Node, Result
from .base import Ctx, cite

SEARCH = "https://efts.sec.gov/LATEST/search-index"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
SRC = "SEC EDGAR"


def company_id(cik: int) -> str:
    return f"company:cik:{cik}"


def search(ctx: Ctx, query: str) -> list[dict]:
    """Companies and filers matching a name or ticker, best first."""
    q = query.strip()
    if q.isdigit():
        return [{"cik": int(q), "name": q, "tickers": "", "score": 100.0}]
    data = ctx.http.get(SEARCH, params={"keysTyped": q})
    out = []
    for h in data.get("hits", {}).get("hits", []):
        src = h["_source"]
        tick = (src.get("tickers") or "").upper()
        label = re.sub(r"\s*\([A-Z0-9.\-, ]+\)$", "", src["entity"])  # drop "(LMT)" ticker tag
        s = 100.0 if q.upper() in tick.split(", ") else names.score(q, label)
        out.append({"cik": int(h["_id"]), "name": src["entity"], "tickers": tick, "score": s})
    return sorted(out, key=lambda x: -x["score"])


def submissions(ctx: Ctx, cik: int, ttl: float = 6 * 3600) -> dict:
    return ctx.http.get(SUBMISSIONS.format(cik=cik), ttl=ttl)


def _addr_text(a: dict) -> str | None:
    if not a or not a.get("street1"):
        return None
    parts = [a.get("street1"), a.get("street2"), a.get("city"), a.get("stateOrCountry"), a.get("zipCode")]
    return ", ".join(p for p in parts if p)


def address_node(text: str, source: dict) -> Node:
    return Node(id="address:" + names.address_key(text), kind=ADDRESS, label=text, sources=[source])


def profile(ctx: Ctx, cik: int) -> Result:
    d = submissions(ctx, cik)
    url = BROWSE.format(cik=cik)
    src = cite(SRC, url)
    recent = d["filings"]["recent"]
    forms = recent.get("form", [])
    counts: dict[str, int] = defaultdict(int)
    for f in forms:
        counts[f] += 1
    former = [f["name"] for f in d.get("formerNames", []) if f.get("name") != d["name"]]
    node = Node(
        id=company_id(cik),
        kind=COMPANY,
        label=d["name"],
        props={
            "cik": cik,
            "tickers": d.get("tickers", []),
            "exchanges": d.get("exchanges", []),
            "sic": d.get("sicDescription"),
            "state_of_incorporation": d.get("stateOfIncorporation"),
            "ein": d.get("ein"),
            "lei": d.get("lei"),
            "phone": d.get("phone"),
            "website": d.get("website"),
            "entity_type": d.get("entityType"),
            "former_names": former,
            "last_filing": recent.get("filingDate", [None])[0],
            "recent_forms": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:8]),
        },
        sources=[src],
    )
    if len(former) >= 3:
        node.flag("name-churn")
    r = Result(nodes=[node])
    for kind, a in (d.get("addresses") or {}).items():
        if text := _addr_text(a):
            an = address_node(text, src)
            r.nodes.append(an)
            r.edges.append(Edge(node.id, an.id, f"{kind}_address", source=src))
    return r


@dataclass
class Insider:
    cik: int
    name: str
    roles: set[str] = field(default_factory=set)
    title: str | None = None
    last_date: str = ""
    bought: float = 0.0  # $ value of open-market purchases (code P)
    sold: float = 0.0  # $ value of open-market sales (code S)
    filings: int = 0


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def parse_ownership_xml(xml: str) -> tuple[list[dict], list[dict]]:
    """Parse a Form 3/4/5 XML into (owners, transactions)."""
    root = ET.fromstring(xml)
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        roles = set()
        for tag, role in (("isDirector", "director"), ("isOfficer", "officer"),
                          ("isTenPercentOwner", "10% owner"), ("isOther", "other")):
            if _text(rel, tag) in ("1", "true"):
                roles.add(role)
        owners.append({
            "cik": int(_text(ro, "reportingOwnerId/rptOwnerCik") or 0),
            "name": _text(ro, "reportingOwnerId/rptOwnerName") or "?",
            "roles": roles,
            "title": _text(rel, "officerTitle"),
        })
    txns = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        try:
            shares = float(_text(t, "transactionAmounts/transactionShares/value") or 0)
            price = float(_text(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            shares = price = 0.0
        txns.append({
            "date": _text(t, "transactionDate/value"),
            "code": _text(t, "transactionCoding/transactionCode"),
            "shares": shares,
            "price": price,
            "value": shares * price,
            "ad": _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value"),
        })
    return owners, txns


def insiders(ctx: Ctx, cik: int, max_filings: int = 40) -> tuple[list[Insider], list[str]]:
    """Insiders from the issuer's most recent ownership filings."""
    d = submissions(ctx, cik)
    rec = d["filings"]["recent"]
    people: dict[int, Insider] = {}
    notes: list[str] = []
    seen = 0
    for i, form in enumerate(rec["form"]):
        if form not in ("3", "4", "5", "4/A") or seen >= max_filings:
            continue
        seen += 1
        doc = rec["primaryDocument"][i].split("/")[-1]  # strip xsl rendering prefix
        acc = rec["accessionNumber"][i].replace("-", "")
        try:
            xml = ctx.http.get(ARCHIVE.format(cik=cik, acc=acc, doc=doc), parse="text", ttl=30 * 86400)
            owners, txns = parse_ownership_xml(xml)
        except Exception as e:  # one bad filing shouldn't sink the list
            notes.append(f"EDGAR: skipped filing {rec['accessionNumber'][i]}: {e}")
            continue
        for o in owners:
            p = people.setdefault(o["cik"], Insider(cik=o["cik"], name=o["name"]))
            p.roles |= o["roles"]
            p.title = p.title or o["title"]
            p.filings += 1
            p.last_date = max(p.last_date, rec["filingDate"][i])
            if len(owners) == 1:  # attribute $ only when unambiguous
                for t in txns:
                    if t["code"] == "P":
                        p.bought += t["value"]
                    elif t["code"] == "S":
                        p.sold += t["value"]
    return sorted(people.values(), key=lambda p: p.last_date, reverse=True), notes


def insiders_result(ctx: Ctx, cik: int, company_label: str) -> Result:
    people, notes = insiders(ctx, cik)
    src = cite(SRC, f"https://www.sec.gov/cgi-bin/own-disp?action=getissuer&CIK={cik:010d}")
    r = Result(notes=notes)
    for p in people:
        is_entity = "10% owner" in p.roles and not ({"officer", "director"} & p.roles) and any(
                s in p.name.upper() for s in (" LLC", " INC", " LP", " CORP", " FUND", " TRUST", " LTD"))
        node = Node(
            id=(f"company:cik:{p.cik}" if is_entity else f"person:cik:{p.cik}"),
            kind=COMPANY if is_entity else PERSON,
            label=p.name,
            props={"cik": p.cik},
            sources=[src],
        )
        role = p.title or ", ".join(sorted(p.roles)) or "insider"
        r.nodes.append(node)
        r.edges.append(Edge(node.id, company_id(cik), "insider_of", props={
            "role": role, "last_filing": p.last_date, "bought_usd": round(p.bought),
            "sold_usd": round(p.sold), "filings": p.filings,
        }, source=src))
    return r

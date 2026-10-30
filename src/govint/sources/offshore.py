"""ICIJ Offshore Leaks (Panama, Paradise, Pandora Papers, Bahamas and Offshore Leaks), loaded locally
from ICIJ's bulk CSVs into their own SQLite file. Licensed ODbL; cite ICIJ when you use it."""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import names
from ..graph import ADDRESS, COMPANY, PERSON, Edge, Node, Result
from .base import Ctx, cite

BULK = "https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip"
NODE_URL = "https://offshoreleaks.icij.org/nodes/{id}"
SRC = "ICIJ Offshore Leaks"

# file -> (kind, name column)
FILES = {
    "nodes-entities.csv": "entity",
    "nodes-officers.csv": "officer",
    "nodes-intermediaries.csv": "intermediary",
    "nodes-addresses.csv": "address",
    "nodes-others.csv": "other",
}
REL_LABEL = {"officer_of": "officer of", "intermediary_of": "intermediary of",
             "registered_address": "registered address", "same_name_as": "same name as",
             "same_company_as": "same company as", "connected_to": "connected to", "underlying": "underlying"}
csv.field_size_limit(10_000_000)


def path(ctx: Ctx) -> Path:
    return ctx.settings.data_dir / "icij.sqlite"


def available(ctx: Ctx) -> bool:
    return path(ctx).exists()


_conns: dict[str, sqlite3.Connection] = {}


def db(ctx: Ctx) -> sqlite3.Connection:
    p = str(path(ctx))
    if p not in _conns:
        c = sqlite3.connect(p, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _conns[p] = c
    return _conns[p]


def _rows(zf: zipfile.ZipFile, member: str):
    with zf.open(member) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline=""))


def load(zip_path: Path, dest: Path, progress=lambda _: None) -> dict[str, int]:
    tmp = dest.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript("""
        pragma journal_mode=off; pragma synchronous=off;
        create table nodes (id integer primary key, kind text, name text, norm text, jurisdiction text,
                            countries text, source text, status text, incorporated text, inactive text,
                            address text, provider text);
        create table rels (a integer, b integer, rel text, link text, start text, end text);
        create virtual table names_fts using fts5(norm, content='', contentless_delete=1);
        create table meta (key text primary key, value text);
    """)
    counts: dict[str, int] = {}
    with zipfile.ZipFile(zip_path) as zf:
        members = {Path(n).name: n for n in zf.namelist()}
        if gen := next((n for n in members if n.startswith("GENERATED_ON_")), None):
            c.execute("insert into meta values ('generated', ?)", (gen.removeprefix("GENERATED_ON_")[:8],))
        for fname, kind in FILES.items():
            progress(f"loading {fname}")
            batch, n = [], 0
            for r in _rows(zf, members[fname]):
                label = r.get("name") or r.get("address") or ""
                norm = names.normalize(label) if kind != "address" else names.address_key(label)
                batch.append((int(r["node_id"]), kind, label.strip(), norm,
                              r.get("jurisdiction_description") or r.get("jurisdiction") or "",
                              r.get("countries") or "", r.get("sourceID") or "", r.get("status") or "",
                              r.get("incorporation_date") or "",
                              r.get("inactivation_date") or r.get("struck_off_date") or r.get("closed_date") or "",
                              r.get("address") or "", r.get("service_provider") or ""))
                if len(batch) >= 20000:
                    n += _insert(c, batch)
                    batch = []
            n += _insert(c, batch)
            counts[kind] = n
        progress("loading relationships")
        batch, n = [], 0
        for r in _rows(zf, members["relationships.csv"]):
            try:
                batch.append((int(r["node_id_start"]), int(r["node_id_end"]), r["rel_type"], r.get("link") or "",
                              r.get("start_date") or "", r.get("end_date") or ""))
            except (KeyError, ValueError):
                continue
            if len(batch) >= 50000:
                c.executemany("insert into rels values (?,?,?,?,?,?)", batch)
                n += len(batch)
                batch = []
        c.executemany("insert into rels values (?,?,?,?,?,?)", batch)
        counts["relationships"] = n + len(batch)
    progress("indexing")
    c.executescript("""
        create index rels_a on rels(a); create index rels_b on rels(b);
        create index nodes_norm on nodes(norm);
    """)
    c.commit()
    c.close()
    tmp.replace(dest)
    _conns.pop(str(dest), None)
    return counts


def _insert(c: sqlite3.Connection, batch: list[tuple]) -> int:
    c.executemany("insert or ignore into nodes values (?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    c.executemany("insert into names_fts(rowid, norm) values (?,?)", [(b[0], b[3]) for b in batch])
    return len(batch)


def sync(ctx: Ctx, progress=lambda _: None) -> dict[str, int]:
    z = ctx.settings.data_dir / "icij-oldb.zip"
    progress("downloading ICIJ Offshore Leaks (~70 MB)")
    ctx.http.download(BULK, z)
    try:
        return load(z, path(ctx), progress)
    finally:
        z.unlink(missing_ok=True)


def meta(ctx: Ctx) -> dict[str, str]:
    if not available(ctx):
        return {}
    return dict(db(ctx).execute("select key, value from meta").fetchall())


# ---------------------------------------------------------------- queries


@dataclass
class Hit:
    node: dict
    score: float
    links: list[dict] = field(default_factory=list)

    @property
    def url(self) -> str:
        return NODE_URL.format(id=self.node["id"])


def _fts_query(norm: str) -> str:
    words = [w for w in norm.split() if len(w) > 1]
    return " ".join(f'"{w}"' for w in words) if words else ""


def search(ctx: Ctx, name: str, threshold: float = 90.0, kinds: tuple[str, ...] | None = None,
           person: bool | None = None, limit: int = 25) -> list[Hit]:
    norm = names.normalize(name)
    q = _fts_query(norm)
    if not q:
        return []
    c = db(ctx)
    rows = c.execute("""select n.* from names_fts f join nodes n on n.id = f.rowid
                        where names_fts match ? limit 3000""", (q,)).fetchall()
    out = []
    for r in rows:
        if r["kind"] == "address" or (kinds and r["kind"] not in kinds):
            continue
        is_person = person if person is not None else r["kind"] == "officer" and not _looks_corporate(r["name"])
        s = names.score(name, r["name"], person=is_person)
        if s >= threshold:
            out.append(Hit(dict(r), s))
    out.sort(key=lambda h: (-h.score, h.node["kind"] != "entity"))
    return out[:limit]


_CORP = re.compile(r"\b(ltd|limited|inc|corp|llc|s\.?a|gmbh|trust|holdings?|foundation|bank|company|co)\b\.?",
                   re.I)


def _looks_corporate(name: str) -> bool:
    return bool(_CORP.search(name or ""))


def links(ctx: Ctx, node_id: int, limit: int = 60) -> list[dict]:
    """Every relationship touching a node, from its point of view."""
    c = db(ctx)
    rows = c.execute("""
        select r.rel, r.link, r.start, r.end, 'out' as dir, n.* from rels r join nodes n on n.id = r.b where r.a = ?
        union all
        select r.rel, r.link, r.start, r.end, 'in' as dir, n.* from rels r join nodes n on n.id = r.a where r.b = ?
        limit ?""", (node_id, node_id, limit)).fetchall()
    return [dict(r) for r in rows]


def node(ctx: Ctx, node_id: int) -> dict | None:
    r = db(ctx).execute("select * from nodes where id=?", (node_id,)).fetchone()
    return dict(r) if r else None


def _address_terms(key: str) -> tuple[list[str], list[str]]:
    """(must-have words, at-least-one-of words) for fuzzy address lookup: house number and street
    name are required; the ZIP or another locality word confirms it."""
    words = key.split()
    must = words[:2] if words and words[0].isdigit() else words[:3]
    rest = [w for w in words[2:] if len(w) > 2 and w not in ("ste", "st", "ave", "rd", "dr", "blvd", "fl")]
    return must, rest


def at_address(ctx: Ctx, text: str, limit: int = 200) -> list[dict]:
    """Entities and officers registered at an address."""
    key = names.address_key(text)
    c = db(ctx)
    addr = c.execute("select id from nodes where kind='address' and norm=?", (key,)).fetchall()
    if not addr:
        must, rest = _address_terms(key)
        q = _fts_query(" ".join(must))
        if not q:
            return []
        cand = c.execute("""select n.id, n.norm from names_fts f join nodes n on n.id = f.rowid
                            where names_fts match ? and n.kind='address' limit 2000""", (q,)).fetchall()
        addr = []
        for r in cand:
            ws = r["norm"].split()
            if must[0].isdigit() and (must[0] not in ws or ws.index(must[0]) + 1 >= len(ws)
                                      or ws[ws.index(must[0]) + 1] != must[1]):
                continue  # house number must sit right before the street name
            if not rest or set(rest) & set(ws):
                addr.append(r)
    ids = [r["id"] for r in addr][:100]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = c.execute(f"""select distinct n.*, a.name as address_text from rels r
                         join nodes n on n.id = r.a join nodes a on a.id = r.b
                         where r.b in ({marks}) and r.rel = 'registered_address' limit ?""", (*ids, limit))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- graph


def _kind(n: dict) -> str:
    if n["kind"] == "address":
        return ADDRESS
    if n["kind"] == "officer" and not _looks_corporate(n["name"]):
        return PERSON
    return COMPANY


def to_node(n: dict) -> Node:
    props = {k: n[k] for k in ("jurisdiction", "countries", "source", "status", "incorporated", "inactive",
                               "provider") if n.get(k)}
    props["icij_role"] = n["kind"]
    return Node(id=f"{_kind(n)}:icij:{n['id']}", kind=_kind(n), label=n["name"], props=props,
                sources=[cite(SRC, NODE_URL.format(id=n["id"]))])


def result(ctx: Ctx, subject: Node) -> Result:
    r = Result()
    if not available(ctx):
        return r
    person = subject.kind == PERSON
    hits = search(ctx, subject.label, threshold=99 if person else 95, person=person, limit=5)
    for h in hits:
        hn = to_node(h.node)
        r.nodes.append(hn)
        r.edges.append(Edge(subject.id, hn.id, "in_offshore_leaks",
                            props={"score": round(h.score, 1), "leak": h.node["source"]}, source=hn.sources[0]))
        if person:
            subject.flag("offshore-leaks-namesake")
        else:
            subject.flag("offshore-leaks" if h.score >= 97 else "offshore-leaks-possible")
        for ln in links(ctx, h.node["id"], limit=25):
            if ln["rel"] in ("same_name_as", "similar", "same_as", "same_id_as"):
                continue
            other = to_node(ln)
            r.nodes.append(other)
            a, b = (hn.id, other.id) if ln["dir"] == "out" else (other.id, hn.id)
            r.edges.append(Edge(a, b, f"icij:{ln['rel']}", props={"start": ln["start"], "end": ln["end"]},
                                source=hn.sources[0]))
    if "offshore-leaks" in subject.flags and "offshore-leaks-possible" in subject.flags:
        subject.flags.remove("offshore-leaks-possible")
    return r

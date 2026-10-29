"""FARA (DOJ): registered agents of foreign principals, mirrored locally from the bulk CSVs."""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from .. import names
from ..graph import COMPANY, PERSON, Edge, Node, Result
from ..http import SourceError
from .base import Ctx, cite

BULK = "https://efile.fara.gov/bulk/zip/{name}.csv.zip"
PAGE = "https://efile.fara.gov/ords/fara/f?p=1381:200:::NO:RP,200:P200_REG_NUMBER:{reg}"
SRC = "FARA (DOJ)"

TABLES = {
    "FARA_All_Registrants": ("fara_registrants", {
        "Registration Number": "reg", "Registration Date": "registered", "Termination Date": "terminated",
        "Name": "name", "Business Name": "business_name", "City": "city", "State": "state"}),
    "FARA_All_ForeignPrincipals": ("fara_principals", {
        "Registration Number": "reg", "Foreign Principal": "principal",
        "Foreign Principal Registration Date": "since", "Foreign Principal Termination Date": "until",
        "Country/Location Represented": "country", "Registrant Name": "registrant"}),
    "FARA_All_ShortForms": ("fara_agents", {
        "Registration Number": "reg", "Short Form Last Name": "last", "Short Form First Name": "first",
        "Short Form Date": "since", "Short Form Termination Date": "until", "Registrant Name": "registrant"}),
}


def _iso(mdy: str) -> str:
    try:
        return datetime.strptime(mdy.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def sync(ctx: Ctx) -> dict[str, int]:
    db = ctx.db()
    counts = {}
    for name, (table, cols) in TABLES.items():
        resp = ctx.http.client.get(BULK.format(name=name), timeout=120)
        if resp.status_code >= 400:
            raise SourceError(f"FARA {name}: HTTP {resp.status_code}")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            text = zf.read(member).decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        dst = list(cols.values())
        db.execute(f"drop table if exists {table}")
        db.execute(f"create table {table} ({', '.join(c + ' text' for c in dst)}, norm text)")
        out = []
        for r in rows:
            rec = {d: (r.get(s) or "").strip() for s, d in cols.items()}
            for k in ("registered", "terminated", "since", "until"):
                if k in rec:
                    rec[k] = _iso(rec[k])
            label = rec.get("name") or rec.get("principal") or f"{rec.get('first', '')} {rec.get('last', '')}"
            out.append([rec[c] for c in dst] + [names.normalize(label)])
        db.executemany(f"insert into {table} values ({','.join('?' * (len(dst) + 1))})", out)
        db.execute(f"create index if not exists {table}_reg on {table}(reg)")
        counts[table] = len(out)
    db.commit()
    return counts


def available(ctx: Ctx) -> bool:
    return ctx.db().execute(
        "select 1 from sqlite_master where type='table' and name='fara_principals'").fetchone() is not None


@dataclass
class Registration:
    reg: str
    registrant: str
    registered: str
    terminated: str
    principals: list[dict] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    matched_as: str = ""  # registrant, principal, or agent
    score: float = 0.0

    @property
    def url(self) -> str:
        return PAGE.format(reg=self.reg)

    @property
    def active(self) -> bool:
        return not self.terminated


def registration(ctx: Ctx, reg: str) -> Registration:
    db = ctx.db()
    r = db.execute("select * from fara_registrants where reg=?", (reg,)).fetchone()
    out = Registration(reg, (r["name"] if r else "") or "?", r["registered"] if r else "",
                       r["terminated"] if r else "")
    out.principals = [dict(x) for x in db.execute(
        "select principal, country, since, until from fara_principals where reg=? order by since desc", (reg,))]
    out.agents = [dict(x) for x in db.execute(
        "select first, last, since, until from fara_agents where reg=? order by since desc", (reg,))]
    if not r and out.principals:
        out.registrant = db.execute("select registrant from fara_principals where reg=?", (reg,)).fetchone()[0]
    return out


def search(ctx: Ctx, name: str, threshold: float = 88.0, person: bool | None = None) -> list[Registration]:
    """Registrations where `name` is the registrant, a foreign principal, or a short-form agent."""
    norm = names.normalize(name)
    if not norm:
        return []
    key = max(norm.split(), key=len)
    db = ctx.db()
    hits: dict[str, tuple[str, float]] = {}

    def consider(reg: str, role: str, label: str, is_person: bool) -> None:
        s = names.score(name, label, person=is_person)
        if s >= threshold and s > hits.get(reg, ("", 0))[1]:
            hits[reg] = (role, s)

    if person is not True:
        for r in db.execute("select reg, name from fara_registrants where norm like ?", (f"%{key}%",)):
            consider(r["reg"], "registrant", r["name"], False)
        for r in db.execute("select reg, principal from fara_principals where norm like ?", (f"%{key}%",)):
            consider(r["reg"], "principal", r["principal"], False)
    if person is not False:
        for r in db.execute("select reg, first, last from fara_agents where norm like ?", (f"%{key}%",)):
            consider(r["reg"], "agent", f"{r['first']} {r['last']}", True)
    out = []
    for reg, (role, s) in sorted(hits.items(), key=lambda kv: -kv[1][1]):
        x = registration(ctx, reg)
        x.matched_as, x.score = role, s
        out.append(x)
    return out


def result(ctx: Ctx, subject: Node) -> Result:
    """Graph edges: subject -> foreign principals it represents, or registrants representing it."""
    r = Result()
    if not available(ctx):
        return r
    for x in search(ctx, subject.label, person=subject.kind == PERSON)[:5]:
        src = cite(SRC, x.url)
        reg_node = Node(id=f"company:fara:{x.reg}", kind=COMPANY, label=x.registrant,
                        props={"fara_registration": x.reg, "registered": x.registered,
                               "terminated": x.terminated}, sources=[src])
        if x.matched_as == "principal":
            r.nodes.append(reg_node)
            r.edges.append(Edge(subject.id, reg_node.id, "represented_by_foreign_agent",
                                props={"score": round(x.score, 1)}, source=src))
            continue
        if x.matched_as == "registrant":
            reg_node = subject
        else:
            r.nodes.append(reg_node)
            r.edges.append(Edge(subject.id, reg_node.id, "fara_agent_at", props={"score": round(x.score, 1)},
                                source=src))
        for p in x.principals[:10]:
            pn = Node(id=f"company:fara-principal:{names.normalize(p['principal'])}", kind=COMPANY,
                      label=p["principal"], props={"country": p["country"]}, sources=[src])
            r.nodes.append(pn)
            r.edges.append(Edge(reg_node.id, pn.id, "foreign_agent_for",
                                props={"country": p["country"], "since": p["since"], "until": p["until"]},
                                source=src))
        if x.active:
            subject.flag("registered-foreign-agent")
    return r


def by_country(ctx: Ctx, country: str, active_only: bool = True) -> list[dict]:
    """Every foreign principal from a country, with the US registrant representing it."""
    q = """select p.reg, p.principal, p.since, p.until, p.registrant, r.terminated
           from fara_principals p left join fara_registrants r on r.reg = p.reg
           where upper(p.country) = upper(?)"""
    if active_only:
        q += " and coalesce(p.until, '') = ''"
    return [dict(r) | {"url": PAGE.format(reg=r["reg"])} for r in ctx.db().execute(q + " order by p.since desc", (country,))]

"""FEC: corporate PACs, where PAC money goes, and what employees give."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .. import names
from ..graph import COMMITTEE, Edge, Node, Result
from ..http import SourceError
from .base import Ctx, cite

API = "https://api.open.fec.gov/v1"
SRC = "FEC"


def cycle_now() -> int:
    y = date.today().year
    return y + (y % 2)


def _get(ctx: Ctx, path: str, **params) -> dict:
    params["api_key"] = ctx.settings.data_gov_key
    try:
        return ctx.http.get(f"{API}{path}", params=params, ttl=12 * 3600)
    except SourceError as e:
        if "429" in str(e) and ctx.settings.data_gov_key == "DEMO_KEY":
            raise SourceError(
                "FEC rate limit hit on the shared DEMO_KEY. Get a free key at "
                "https://api.data.gov/signup/ and set GOVINT_DATA_GOV_KEY."
            ) from e
        raise


def committee_url(cid: str) -> str:
    return f"https://www.fec.gov/data/committee/{cid}/"


def committees(ctx: Ctx, name: str, threshold: float = 80.0) -> list[dict]:
    """Committees whose name contains the org name (corporate PACs, mostly)."""
    data = _get(ctx, "/names/committees/", q=name)
    norm = names.normalize(name)
    out = []
    for c in data.get("results", []):
        cn = names.normalize(c["name"])
        # PAC names are long ("ACME CORP EMPLOYEES POLITICAL ACTION COMMITTEE"); require containment.
        if norm and (norm in cn or names.score(name, c["name"]) >= threshold):
            out.append(c)
    return out


def pac_recipients(ctx: Ctx, committee_id: str, cycle: int | None = None, pages: int = 2) -> list[dict]:
    """Where a committee's money went this cycle, aggregated by recipient."""
    cycle = cycle or cycle_now()
    agg: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "count": 0, "committee_id": None})
    params = {"committee_id": committee_id, "two_year_transaction_period": cycle, "per_page": 100,
              "sort": "-disbursement_amount"}
    for _ in range(pages):
        data = _get(ctx, "/schedules/schedule_b/", **params)
        for d in data.get("results", []):
            a = agg[d.get("recipient_name") or "?"]
            a["total"] += d.get("disbursement_amount") or 0
            a["count"] += 1
            a["committee_id"] = a["committee_id"] or d.get("recipient_committee_id")
        last = (data.get("pagination") or {}).get("last_indexes")
        if not last or len(data.get("results", [])) < 100:
            break
        params.update(last)
    return sorted(({"name": k, **v} for k, v in agg.items()), key=lambda x: -x["total"])


def employer_giving(ctx: Ctx, employer: str, cycle: int | None = None) -> list[dict]:
    """Donations by people listing this employer, merged across employer spellings."""
    data = _get(ctx, "/schedules/schedule_a/by_employer/", employer=employer,
                cycle=cycle or cycle_now(), per_page=100, sort="-total")
    merged: dict[str, dict] = {}
    for g in data.get("results", []):
        m = merged.setdefault(g["committee_id"], {"committee_id": g["committee_id"], "total": 0.0,
                                                    "count": 0, "employers": [], "cycle": g.get("cycle")})
        m["total"] += g.get("total") or 0
        m["count"] += g.get("count") or 0
        if g["employer"] not in m["employers"]:
            m["employers"].append(g["employer"])
    for m in merged.values():
        m["employer"] = "; ".join(m["employers"])
    return sorted(merged.values(), key=lambda m: -m["total"])


def committee_names(ctx: Ctx, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    data = _get(ctx, "/committees/", committee_id=ids[:50], per_page=50)
    return {c["committee_id"]: c["name"] for c in data.get("results", [])}


def contributions_by_person(ctx: Ctx, name: str, limit: int = 50) -> list[dict]:
    data = _get(ctx, "/schedules/schedule_a/", contributor_name=name, per_page=limit,
                sort="-contribution_receipt_date", is_individual="true")
    return data.get("results", [])


def result(ctx: Ctx, subject: Node, cycle: int | None = None) -> Result:
    cycle = cycle or cycle_now()
    r = Result()
    for c in committees(ctx, subject.label)[:3]:
        src = cite(SRC, committee_url(c["id"]))
        cn = Node(id=f"committee:{c['id']}", kind=COMMITTEE, label=c["name"],
                  props={"committee_id": c["id"], "active": c.get("is_active")}, sources=[src])
        r.nodes.append(cn)
        r.edges.append(Edge(subject.id, cn.id, "sponsors_pac", source=src))
        if not c.get("is_active"):
            continue
        for rec in pac_recipients(ctx, c["id"], cycle, pages=1)[:10]:
            rid = rec["committee_id"]
            rn = Node(id=f"committee:{rid}" if rid else f"committee:name:{names.normalize(rec['name'])}",
                      kind=COMMITTEE, label=rec["name"], props={"committee_id": rid},
                      sources=[cite(SRC, committee_url(rid)) if rid else src])
            r.nodes.append(rn)
            r.edges.append(Edge(cn.id, rn.id, "gave_to",
                                props={"usd": round(rec["total"]), "count": rec["count"], "cycle": cycle},
                                source=src))
    giving = employer_giving(ctx, subject.label, cycle)
    labels = committee_names(ctx, [g["committee_id"] for g in giving[:10]])
    for g in giving[:10]:
        cid = g["committee_id"]
        src = cite(SRC, f"https://www.fec.gov/data/receipts/individual-contributions/?contributor_employer={g['employer']}&two_year_transaction_period={cycle}")
        cn = Node(id=f"committee:{cid}", kind=COMMITTEE, label=labels.get(cid, cid),
                  props={"committee_id": cid}, sources=[cite(SRC, committee_url(cid))])
        r.nodes.append(cn)
        r.edges.append(Edge(subject.id, cn.id, "employees_gave_to",
                            props={"usd": round(g["total"]), "count": g["count"], "cycle": cycle,
                                   "employer_string": g["employer"]}, source=src))
    return r

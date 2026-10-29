"""USAspending.gov: federal contracts and grants by recipient."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .. import names
from ..graph import ADDRESS, AGENCY, AWARD, COMPANY, Edge, Node, Result
from .base import Ctx, cite

API = "https://api.usaspending.gov/api/v2"
SRC = "USAspending.gov"
CONTRACTS = ["A", "B", "C", "D"]
GRANTS = ["02", "03", "04", "05"]


def recipients(ctx: Ctx, name: str, threshold: float = 88.0) -> list[dict]:
    data = ctx.http.post(f"{API}/recipient/", {"keyword": name, "limit": 50, "award_type": "all"})
    out = []
    for r in data.get("results", []):
        s = names.score(name, r["name"])
        if s >= threshold:
            out.append({**r, "score": s})
    return sorted(out, key=lambda r: (-r["score"], -(r.get("amount") or 0)))


def recipient_profile(ctx: Ctx, recipient_id: str) -> dict:
    return ctx.http.get(f"{API}/recipient/{recipient_id}/", params={"year": "all"})


def awards(ctx: Ctx, name: str, years: int = 5, kinds: list[str] = CONTRACTS, limit: int = 25) -> dict:
    today = date.today()
    start = date(today.year - years, 10, 1) if years else date(2007, 10, 1)
    body = {
        "filters": {
            "recipient_search_text": [name],
            "award_type_codes": kinds,
            "time_period": [{"start_date": start.isoformat(), "end_date": today.isoformat()}],
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
                   "Awarding Sub Agency", "Start Date", "End Date", "Description",
                   "Recipient UEI", "generated_internal_id"],
        "limit": limit,
        "sort": "Award Amount",
        "order": "desc",
    }
    return ctx.http.post(f"{API}/search/spending_by_award/", body)


def award_url(gid: str) -> str:
    return f"https://www.usaspending.gov/award/{gid}"


def result(ctx: Ctx, subject: Node, years: int = 5) -> Result:
    """Recipient identities (UEIs, parents, addresses) plus top contracts and grants."""
    r = Result()
    recs = recipients(ctx, subject.label)
    if not recs:
        return r
    # Profile the top recipient records (by score, then total dollars).
    for rec in recs[:3]:
        prof = recipient_profile(ctx, rec["id"])
        url = f"https://www.usaspending.gov/recipient/{rec['id']}/latest"
        src = cite(SRC, url)
        uei = prof.get("uei") or rec.get("uei")
        node = Node(
            id=f"company:uei:{uei}" if uei else f"company:usasp:{rec['id']}",
            kind=COMPANY,
            label=prof.get("name") or rec["name"],
            props={
                "uei": uei, "duns": prof.get("duns"), "recipient_level": prof.get("recipient_level"),
                "federal_total_usd": round(prof.get("total_transaction_amount") or rec.get("amount") or 0),
                "business_types": prof.get("business_types"),
                "alternate_names": prof.get("alternate_names"),
            },
            sources=[src],
        )
        r.nodes.append(node)
        r.edges.append(Edge(subject.id, node.id, "same_as", props={"score": round(rec["score"], 1)}, source=src))
        loc = prof.get("location") or {}
        if loc.get("address_line1"):
            text = ", ".join(x for x in (loc.get("address_line1"), loc.get("city_name"),
                                         loc.get("state_code"), loc.get("zip")) if x)
            an = Node(id="address:" + names.address_key(text), kind=ADDRESS, label=text, sources=[src])
            r.nodes.append(an)
            r.edges.append(Edge(node.id, an.id, "business_address", source=src))
        # Only the current parent; the "parents" list includes stale historical links.
        puei = prof.get("parent_uei")
        if puei and puei != uei:
            pn = Node(id=f"company:uei:{puei}", kind=COMPANY, label=prof.get("parent_name") or puei,
                      props={"uei": puei},
                      sources=[cite(SRC, f"https://www.usaspending.gov/recipient/{prof['parent_id']}/latest")
                               if prof.get("parent_id") else src])
            r.nodes.append(pn)
            r.edges.append(Edge(node.id, pn.id, "child_of", source=src))

    # Top awards across all matched recipients, linked to the agencies paying them.
    for kinds, label in ((CONTRACTS, "contract"), (GRANTS, "grant")):
        data = awards(ctx, subject.label, years=years, kinds=kinds, limit=10)
        for a in data.get("results", []):
            if not _same_family(subject.label, a["Recipient Name"]):
                continue
            src = cite(SRC, award_url(a["generated_internal_id"]))
            an = Node(
                id=f"award:{a['generated_internal_id']}",
                kind=AWARD,
                label=a["Award ID"],
                props={"type": label, "amount": a["Award Amount"], "start": a["Start Date"],
                       "end": a["End Date"], "description": a["Description"],
                       "sub_agency": a["Awarding Sub Agency"], "recipient": a["Recipient Name"]},
                sources=[src],
            )
            ag = Node(id=f"agency:{names.normalize(a['Awarding Agency'] or 'unknown')}", kind=AGENCY,
                      label=a["Awarding Agency"] or "Unknown agency", sources=[cite(SRC, "https://www.usaspending.gov")])
            r.nodes += [an, ag]
            r.edges.append(Edge(ag.id, an.id, "awarded", source=src))
            r.edges.append(Edge(an.id, subject.id, "paid_to", props={"amount": a["Award Amount"]}, source=src))
    return r


def _same_family(query: str, recipient: str) -> bool:
    """Keep awards to the matched company and its name-alike subsidiaries ("Palantir USG Inc")."""
    if names.score(query, recipient) >= 85:
        return True
    q, r = names.normalize(query).split(), names.normalize(recipient).split()
    return bool(q and r and q[0] == r[0] and len(q[0]) >= 4)


def summarize_by_agency(data: dict) -> list[tuple[str, float, int]]:
    tot: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    for a in data.get("results", []):
        t = tot[a["Awarding Agency"] or "?"]
        t[0] += a["Award Amount"] or 0
        t[1] += 1
    return sorted(((k, v[0], int(v[1])) for k, v in tot.items()), key=lambda x: -x[1])

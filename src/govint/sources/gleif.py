"""GLEIF: Legal Entity Identifiers and corporate parent/child chains, worldwide."""

from __future__ import annotations

from .. import names
from ..graph import ADDRESS, COMPANY, Edge, Node, Result
from ..http import SourceError
from .base import Ctx, cite

API = "https://api.gleif.org/api/v1"
SRC = "GLEIF"


def lei_url(lei: str) -> str:
    return f"https://search.gleif.org/#/record/{lei}"


def search(ctx: Ctx, name: str, threshold: float = 88.0) -> list[dict]:
    data = ctx.http.get(f"{API}/fuzzycompletions", params={"field": "entity.legalName", "q": name})
    ids = [d["relationships"]["lei-records"]["data"]["id"] for d in data.get("data", [])
           if names.score(name, d["attributes"]["value"]) >= threshold]
    if not ids:
        return []
    recs = records(ctx, ids)
    return sorted(recs, key=lambda r: -names.score(name, r["attributes"]["entity"]["legalName"]["name"]))


def records(ctx: Ctx, leis: list[str]) -> list[dict]:
    data = ctx.http.get(f"{API}/lei-records", params={"filter[lei]": ",".join(leis), "page[size]": 100})
    return data.get("data", [])


def _rel(ctx: Ctx, lei: str, which: str, limit: int = 50) -> list[dict]:
    try:
        data = ctx.http.get(f"{API}/lei-records/{lei}/{which}", params={"page[size]": limit})
    except SourceError as e:
        if "404" in str(e):  # no parent reported, or a reporting exception
            return []
        raise
    d = data.get("data")
    if d is None:
        return []
    return d if isinstance(d, list) else [d]


def node_for(rec: dict) -> list[Node]:
    a = rec["attributes"]
    e = a["entity"]
    addr = e.get("headquartersAddress") or e.get("legalAddress") or {}
    src = cite(SRC, lei_url(rec["id"]))
    n = Node(
        id=f"company:lei:{rec['id']}",
        kind=COMPANY,
        label=e["legalName"]["name"],
        props={
            "lei": rec["id"], "jurisdiction": e.get("jurisdiction"), "status": e.get("status"),
            "legal_form": (e.get("legalForm") or {}).get("id"), "category": e.get("category"),
            "registered_as": e.get("registeredAs"),
            "registration_status": (a.get("registration") or {}).get("status"),
            "other_names": [o["name"] for o in e.get("otherNames") or []],
            "country": addr.get("country"),
        },
        sources=[src],
    )
    out = [n]
    if addr.get("addressLines"):
        text = ", ".join([*addr["addressLines"], addr.get("city") or "", addr.get("region") or "",
                          addr.get("postalCode") or "", addr.get("country") or ""])
        text = ", ".join(p for p in (x.strip() for x in text.split(",")) if p)
        out.append(Node(id="address:" + names.address_key(text), kind=ADDRESS, label=text, sources=[src]))
    return out


def tree_result(ctx: Ctx, rec: dict, children_limit: int = 25) -> Result:
    nodes = node_for(rec)
    me = nodes[0]
    r = Result(nodes=nodes)
    if len(nodes) > 1:
        r.edges.append(Edge(me.id, nodes[1].id, "headquarters_address", source=me.sources[0]))
    for which, rel in (("direct-parent", "child_of"), ("ultimate-parent", "ultimately_owned_by")):
        for p in _rel(ctx, rec["id"], which):
            pn = node_for(p)[0]
            r.nodes.append(pn)
            r.edges.append(Edge(me.id, pn.id, rel, source=pn.sources[0]))
            if pn.props.get("country") and me.props.get("country") and pn.props["country"] != me.props["country"]:
                me.flag(f"foreign-parent:{pn.props['country']}")
    for c in _rel(ctx, rec["id"], "direct-children", children_limit):
        cn = node_for(c)[0]
        r.nodes.append(cn)
        r.edges.append(Edge(cn.id, me.id, "child_of", source=cn.sources[0]))
    return r


def result(ctx: Ctx, subject: Node, lei: str | None = None) -> Result:
    recs = records(ctx, [lei]) if lei else search(ctx, subject.label)[:1]
    r = Result()
    for rec in recs:
        t = tree_result(ctx, rec)
        r.extend(t)
        r.edges.append(Edge(subject.id, t.nodes[0].id, "same_as", source=t.nodes[0].sources[0]))
    return r

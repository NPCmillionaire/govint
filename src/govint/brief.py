"""Turn a pivot graph into a sourced markdown dossier."""

from __future__ import annotations

from datetime import date

from .graph import ADDRESS, COMPANY, PERSON, SANCTION, Graph, Node

FLAG_TEXT = {
    "sanctions-match": "Name matches a US sanctions/export-control list entry (score ≥ 95)",
    "sanctions-possible": "Name resembles a US sanctions/export-control list entry; verify",
    "sam-excluded": "SAM.gov registration shows an active exclusion",
    "sam-exclusion-possible": "Name resembles a SAM.gov exclusion record; verify",
    "name-churn": "Three or more former names on file with the SEC",
    "listed": "Appears on a screening list",
}


def _link(n: Node) -> str:
    for s in n.sources:
        if s.get("url"):
            return f"[{n.label}]({s['url']})"
    return n.label


def _usd(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "?"


def _flag_text(f: str) -> str:
    if f.startswith("foreign-parent:"):
        return f"Parent company is registered outside its home country ({f.split(':', 1)[1]})"
    return FLAG_TEXT.get(f, f)


def identities(g: Graph, seed: Node) -> list[Node]:
    """The seed plus everything linked to it by same_as."""
    out = [seed]
    for e, n in g.neighbors(seed.id):
        if e.rel == "same_as" and n not in out:
            out.append(n)
    return out


def render(g: Graph, seed: Node) -> str:
    ids = identities(g, seed)
    id_set = {n.id for n in ids}
    L: list[str] = [f"# {seed.label}", "", f"_govint brief, {date.today().isoformat()}. "
                    "Automated from public records; name matches are fuzzy, so verify before relying on any line._", ""]

    # Flags first.
    flagged = [n for n in g.nodes.values() if n.flags and n.kind != SANCTION]
    L.append("## Flags")
    if flagged:
        for n in flagged:
            for f in n.flags:
                L.append(f"- **{_flag_text(f)}**: {_link(n)}")
    else:
        L.append("- None raised by the sources checked.")
    L.append("")

    # Identity
    L.append("## Identity")
    for n in ids[1:]:
        p = n.props
        bits = []
        for k, lbl in (("cik", "CIK"), ("tickers", "Tickers"), ("lei", "LEI"), ("uei", "UEI"),
                       ("cage", "CAGE"), ("ein", "EIN"), ("jurisdiction", "Jurisdiction"),
                       ("state_of_incorporation", "Incorporated"), ("sic", "Industry"),
                       ("entity_start", "Started")):
            v = p.get(k)
            if v:
                bits.append(f"{lbl}: {', '.join(v) if isinstance(v, list) else v}")
        L.append(f"- {_link(n)}" + (f": {'; '.join(bits)}" if bits else ""))
        if p.get("former_names"):
            L.append(f"  - Former names: {', '.join(p['former_names'][:8])}")
    if len(ids) == 1:
        L.append("- No confident match in EDGAR, GLEIF, USAspending or SAM.")
    addrs = {n.id: n for i in ids for e, n in g.neighbors(i.id) if n.kind == ADDRESS}
    if addrs:
        L.append("- Addresses: " + "; ".join(a.label for a in addrs.values()))
    L.append("")

    # Ownership
    parents, children = [], []
    for e in g.edges.values():
        if e.rel in ("child_of", "ultimately_owned_by"):
            if e.src in id_set and e.dst not in id_set:
                if all(p.id != e.dst for _, p in parents):
                    parents.append((e.rel, g.nodes[e.dst]))
            elif e.dst in id_set and e.src not in id_set:
                if g.nodes[e.src] not in children:
                    children.append(g.nodes[e.src])
    if parents or children:
        L.append("## Corporate structure")
        for rel, p in parents:
            tag = "Ultimate parent" if rel == "ultimately_owned_by" else "Parent"
            where = p.props.get("jurisdiction") or p.props.get("country")
            L.append(f"- {tag}: {_link(p)}" + (f" ({where})" if where else ""))
        if children:
            L.append(f"- Subsidiaries reporting to GLEIF ({len(children)} shown):")
            for c in children[:25]:
                L.append(f"  - {_link(c)} ({c.props.get('jurisdiction') or c.props.get('country') or '?'})")
        L.append("")

    # People
    insiders = [(e, g.nodes[e.src]) for e in g.edges.values() if e.rel == "insider_of" and e.dst in id_set]
    if insiders:
        L.append("## Insiders (SEC Forms 3/4/5)")
        L.append("| Name | Role | Last filing | Open-market buys | Open-market sells |")
        L.append("|---|---|---|---|---|")
        for e, p in sorted(insiders, key=lambda x: x[0].props.get("last_filing") or "", reverse=True):
            fl = " ⚑" if p.flags else ""
            L.append(f"| {_link(p)}{fl} | {e.props.get('role')} | {e.props.get('last_filing')} | "
                     f"{_usd(e.props.get('bought_usd')) if e.props.get('bought_usd') else ''} | "
                     f"{_usd(e.props.get('sold_usd')) if e.props.get('sold_usd') else ''} |")
        L.append("")

    # Money
    awards = [(e, g.nodes[e.src]) for e in g.edges.values() if e.rel == "paid_to" and e.dst in id_set]
    totals = [n.props.get("federal_total_usd") for n in ids if n.props.get("federal_total_usd")]
    if awards or totals:
        L.append("## Federal money (USAspending)")
        if totals:
            L.append(f"- Lifetime federal obligations across matched recipient records: {_usd(sum(totals))}")
        if awards:
            L.append("")
            L.append("| Award | Type | Amount | Agency | Start | Description |")
            L.append("|---|---|---|---|---|---|")
            for _e, a in sorted(awards, key=lambda x: -(x[1].props.get("amount") or 0))[:15]:
                desc = (a.props.get("description") or "").replace("|", "/")[:80]
                L.append(f"| {_link(a)} | {a.props.get('type')} | {_usd(a.props.get('amount'))} | "
                         f"{a.props.get('sub_agency')} | {a.props.get('start')} | {desc} |")
        L.append("")

    # Politics
    pacs = [g.nodes[e.dst] for e in g.edges.values() if e.rel == "sponsors_pac" and e.src in id_set]
    emp = [(e, g.nodes[e.dst]) for e in g.edges.values() if e.rel == "employees_gave_to" and e.src in id_set]
    if pacs or emp:
        L.append("## Political money (FEC)")
        for p in pacs:
            L.append(f"- PAC: {_link(p)}")
            gave = [(e, g.nodes[e.dst]) for e in g.edges.values() if e.rel == "gave_to" and e.src == p.id]
            for e, r in sorted(gave, key=lambda x: -x[0].props.get("usd", 0))[:10]:
                L.append(f"  - {_usd(e.props['usd'])} → {_link(r)} ({e.props['count']} payments, {e.props['cycle']} cycle)")
        if emp:
            L.append(f"- Individual donations listing this employer ({emp[0][0].props['cycle']} cycle):")
            for e, c in sorted(emp, key=lambda x: -x[0].props["usd"])[:10]:
                L.append(f"  - {_usd(e.props['usd'])} → {_link(c)} ({e.props['count']} donations)")
        L.append("")

    # Sanctions
    hits = [(e, g.nodes[e.src], g.nodes[e.dst]) for e in g.edges.values()
            if e.rel == "possible_match" and g.nodes[e.dst].kind == SANCTION]
    L.append("## Sanctions & exclusions screening")
    if hits:
        for e, who, s in sorted(hits, key=lambda x: -(x[0].props.get("score") or 0)):
            L.append(f"- {who.label} ↔ {_link(s)}: {s.props.get('list')}; programs "
                     f"{', '.join(s.props.get('programs') or []) or '?'}; score {e.props.get('score', '?')}")
    else:
        screened = sum(1 for n in g.nodes.values() if n.kind in (COMPANY, PERSON))
        L.append(f"- No matches across {screened} screened names." if not any(
            "CSL not synced" in x for x in g.notes) else "- Not screened (CSL not synced).")
    L.append("")

    # Aircraft
    planes = [(e, g.nodes[e.src], g.nodes[e.dst]) for e in g.edges.values() if e.rel == "registered_owner_of"]
    if planes:
        L.append("## Aircraft (FAA registry)")
        for _e, owner, a in planes:
            p = a.props
            L.append(f"- {_link(a)}: registered to {p.get('name')} ({p.get('registrant_type')}), "
                     f"{p.get('city')}, {p.get('state')}; Mode S {p.get('mode_s_hex')}; matched via {owner.label}")
        L.append("")

    if g.notes:
        L.append("## Coverage notes")
        L += [f"- {n}" for n in g.notes]
        L.append("")

    counts = {}
    for n in g.nodes.values():
        counts[n.kind] = counts.get(n.kind, 0) + 1
    L.append(f"_Graph: {len(g.nodes)} nodes ({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))}), "
             f"{len(g.edges)} edges._")
    return "\n".join(L) + "\n"


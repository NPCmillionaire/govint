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
    "registered-foreign-agent": "Active registration under the Foreign Agents Registration Act",
    "offshore-leaks": "Name appears in the ICIJ Offshore Leaks database",
    "offshore-leaks-possible": "Name resembles an entry in the ICIJ Offshore Leaks database; verify",
    "offshore-leaks-namesake": "Someone with this exact name is in the ICIJ Offshore Leaks database (may be a different person)",
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
    if f.startswith("court:"):
        return f"Party to a federal case tagged {f.split(':', 1)[1]} (see Court records)"
    if f.startswith("ppp:"):
        return f"PPP loan: {f.split(':', 1)[1]}"
    if f.startswith("revolving-door:"):
        return f"{f.split(':', 1)[1]} of its lobbyists previously held government jobs (see Lobbying)"
    if f.startswith("lobbying-foreign-interest:"):
        return f"Lobbying filings disclose a foreign entity with an interest: {f.split(':', 1)[1]}"
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
        shown: set[str] = set()  # one line per flag across the subject's own identities
        for n in flagged:
            for f in n.flags:
                if n.id in id_set:
                    if f in shown:
                        continue
                    shown.add(f)
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

    # FARA
    fara_e = [e for e in g.edges.values() if e.rel in ("foreign_agent_for", "fara_agent_at",
                                                        "represented_by_foreign_agent")]
    if fara_e:
        L.append("## Foreign agents (FARA)")
        for e in fara_e:
            a, b = g.nodes[e.src], g.nodes[e.dst]
            if e.rel == "foreign_agent_for":
                end = f" until {e.props['until']}" if e.props.get("until") else " (active)"
                L.append(f"- {_link(a)} represents {_link(b)} ({e.props.get('country')}), since "
                         f"{e.props.get('since')}{end}")
            elif e.rel == "fara_agent_at":
                L.append(f"- {a.label} is a registered agent at {_link(b)}")
            else:
                L.append(f"- {a.label} is represented in the US by {_link(b)}")
        L.append("")

    # Congress
    ctrades = [(e, g.nodes[e.src]) for e in g.edges.values()
               if e.rel.startswith("congress_trade:") and e.dst in id_set]
    if ctrades:
        L.append("## Congressional trades (House PTRs)")
        L.append("| Member | Trade date | Filed | Type | Amount | Owner |")
        L.append("|---|---|---|---|---|---|")
        for e, m in sorted(ctrades, key=lambda x: x[0].props["date"], reverse=True)[:25]:
            p = e.props
            L.append(f"| [{m.label}]({e.source.get('url', '')}) | {p['date']} | {p['filed']} | {p['type']} | "
                     f"{p['amount']} | {p['owner']} |")
        if len(ctrades) > 25:
            L.append(f"\n_{len(ctrades) - 25} more; run `govint congress trades --ticker <TICKER>`._")
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

    # Lobbying
    firms = [(e, g.nodes[e.dst]) for e in g.edges.values() if e.rel == "pays_lobbyist" and e.src in id_set]
    ex = [(e, g.nodes[e.src]) for e in g.edges.values() if e.rel == "lobbies_for" and e.dst in id_set]
    spent = sum(n.props.get("lobbying_last_2y") or 0 for n in ids)
    if firms or ex or spent:
        L.append("## Lobbying (LDA)")
        if spent:
            L.append(f"- Reported lobbying spend, last two years (in-house plus outside firms): {_usd(spent)}")
        for e, f in sorted(firms, key=lambda x: -x[0].props.get("amount", 0)):
            L.append(f"- Pays {_link(f)}: {_usd(e.props.get('amount'))} (latest {e.props.get('last')})")
        if ex:
            L.append(f"- Lobbyists who held government jobs ({len(ex)}):")
            for e, p in ex[:15]:
                L.append(f"  - {_link(p)} ({p.props.get('firm')}): {e.props.get('former_position')}")
        L.append("")

    # PPP
    loans = [(e, g.nodes[e.src]) for e in g.edges.values() if e.rel == "ppp_loan_to" and e.dst in id_set]
    if loans:
        seen_l: set[str] = set()
        L.append("## PPP loans (SBA)")
        L.append("| Borrower | Approved | Amount | Forgiven | Jobs | Lender | Notes |")
        L.append("|---|---|---|---|---|---|---|")
        for e, ln in sorted(loans, key=lambda x: x[1].props.get("approved") or ""):
            if ln.id in seen_l:
                continue
            seen_l.add(ln.id)
            p = ln.props
            notes = "; ".join(f.split(":", 1)[1] for f in ln.flags)
            L.append(f"| {e.props.get('borrower')} ({p.get('city')}, {p.get('state')}) | {p.get('approved')} | "
                     f"{_usd(p.get('amount'))} | {_usd(p.get('forgiven')) if p.get('forgiven') else ''} | "
                     f"{int(p['jobs']) if p.get('jobs') else ''} | {p.get('lender')} | {notes} |")
        L.append("")

    # Courts
    cases = [(e, g.nodes[e.dst]) for e in g.edges.values() if e.rel == "party_to" and e.src in id_set]
    if cases:
        seen_c: set[str] = set()
        L.append("## Court records (federal, via CourtListener)")
        for e, c in sorted(cases, key=lambda x: x[1].props.get("filed") or "", reverse=True):
            if c.id in seen_c:
                continue
            seen_c.add(c.id)
            p = c.props
            tags = f" **[{', '.join(p['tags'])}]**" if p.get("tags") else ""
            L.append(f"- {p.get('filed')}: {_link(c)}, {p.get('court')} {p.get('docket')}"
                     f"{', ' + p['nature'] if p.get('nature') else ''}{tags}")
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

    # Offshore
    off = [(e, g.nodes[e.src], g.nodes[e.dst]) for e in g.edges.values() if e.rel == "in_offshore_leaks"]
    if off:
        L.append("## Offshore Leaks (ICIJ)")
        seen_x: set[str] = set()
        for e, who, x in sorted(off, key=lambda t: -(t[0].props.get("score") or 0)):
            if x.id in seen_x:
                continue
            seen_x.add(x.id)
            p = x.props
            where = p.get("jurisdiction") or p.get("countries") or "?"
            L.append(f"- {who.label} ↔ {_link(x)}: {p.get('icij_role')} in {e.props.get('leak')}, {where}; "
                     f"score {e.props.get('score')}")
            rel = [e2 for e2 in g.edges.values() if e2.rel.startswith("icij:") and x.id in (e2.src, e2.dst)]
            for e2 in rel[:10]:
                other = g.nodes[e2.dst if e2.src == x.id else e2.src]
                L.append(f"  - {e2.rel[5:].replace('_', ' ')}: {_link(other)}")
            if len(rel) > 10:
                L.append(f"  - _{len(rel) - 10} more; run `govint offshore {x.id.rsplit(':', 1)[1]}`_")
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


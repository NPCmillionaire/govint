"""govint command line."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.tree import Tree

from . import __version__, brief, config, names
from .graph import Graph, Node
from .http import Http, SourceError
from .pivot import pivot as run_pivot
from .sources import csl, edgar, faa, fec, gleif, sam, usaspending
from .sources.base import Ctx

app = typer.Typer(help="Public-records OSINT across US government databases.", no_args_is_help=True,
                  add_completion=False, rich_markup_mode="rich")
out = Console()
err = Console(stderr=True)
STATE: dict = {"cache": True, "json": False}


def ctx() -> Ctx:
    s = config.load()
    return Ctx(settings=s, http=Http(s.user_agent, s.cache_path, use_cache=STATE["cache"]))


def die(msg: str) -> None:
    err.print(f"[red]error:[/] {msg}")
    raise typer.Exit(1)


def emit_json(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def usd(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return ""


@app.callback(invoke_without_command=True)
def main_opts(
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the local HTTP cache."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    version: bool = typer.Option(False, "--version", is_eager=True, help="Show version."),
):
    if version:
        out.print(__version__)
        raise typer.Exit()
    STATE["cache"] = not no_cache
    STATE["json"] = as_json


# ---------------------------------------------------------------- setup


CONFIG_KEYS = {"contact": "Email for the SEC User-Agent (required by SEC)",
               "data_gov_key": "api.data.gov key for FEC (https://api.data.gov/signup/)",
               "sam_key": "SAM.gov API key (sam.gov > Account Details)"}


@app.command("config")
def config_cmd(key: Optional[str] = typer.Argument(None), value: Optional[str] = typer.Argument(None)):
    """Show settings, or set one: [b]govint config contact you@example.com[/]"""
    path = config.config_path()
    if key is None:
        s = config.load()
        t = Table(title=f"govint config ({path})", show_header=False)
        t.add_row("contact", s.contact or "[red]unset — SEC lookups will fail[/]")
        t.add_row("data_gov_key", "DEMO_KEY (tight rate limit)" if s.data_gov_key == "DEMO_KEY" else "set")
        t.add_row("sam_key", "set" if s.sam_key else "[yellow]unset — SAM.gov skipped[/]")
        t.add_row("data dir", str(s.data_dir))
        out.print(t)
        return
    if key not in CONFIG_KEYS:
        die(f"unknown key {key!r}; choose from {', '.join(CONFIG_KEYS)}")
    if value is None:
        die("missing value")
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.split("=")[0].strip() == key]
    lines.append(f"{key} = {json.dumps(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    out.print(f"set {key} in {path}")


@app.command()
def sync(
    dataset: str = typer.Argument("all", help="csl, faa, or all"),
    file: Optional[Path] = typer.Option(None, "--file", help="Use an already-downloaded FAA zip."),
):
    """Download bulk datasets for offline screening (CSL sanctions, FAA registry)."""
    c = ctx()
    failed = False
    if dataset in ("csl", "all"):
        with err.status("downloading Consolidated Screening List…"):
            try:
                out.print(f"CSL: {csl.sync(c):,} records")
            except SourceError as e:
                err.print(f"[red]CSL failed:[/] {e}")
                failed = True
    if dataset in ("faa", "all"):
        with err.status("loading FAA registry…"):
            try:
                out.print(f"FAA: {faa.sync(c, file):,} aircraft")
            except SourceError as e:
                err.print(f"[red]FAA failed:[/] {e}")
                failed = True
    if dataset not in ("csl", "faa", "all"):
        die("dataset must be csl, faa, or all")
    if failed:
        raise typer.Exit(1)


@app.command()
def status():
    """What's synced and configured."""
    c = ctx()
    s = c.settings
    t = Table(show_header=False)
    p = csl.path(c)
    t.add_row("CSL sanctions", f"synced {_age(p)}" if p.exists() else "[yellow]not synced[/] (govint sync csl)")
    if faa.available(c):
        n = c.db().execute("select count(*) from faa_master").fetchone()[0]
        t.add_row("FAA registry", f"{n:,} aircraft, synced {_age(s.db_path)}")
    else:
        t.add_row("FAA registry", "[yellow]not synced[/] (govint sync faa)")
    t.add_row("SEC contact", s.contact or "[red]unset[/] (govint config contact you@example.com)")
    t.add_row("FEC key", "DEMO_KEY" if s.data_gov_key == "DEMO_KEY" else "set")
    t.add_row("SAM key", "set" if s.sam_key else "[yellow]unset[/]")
    out.print(t)


def _age(p: Path) -> str:
    import time
    days = (time.time() - p.stat().st_mtime) / 86400
    return "today" if days < 1 else f"{days:.0f}d ago"


# ---------------------------------------------------------------- lookups


@app.command()
def screen(
    name: Optional[str] = typer.Argument(None),
    file: Optional[Path] = typer.Option(None, "--csv", help="CSV of names to screen in bulk."),
    column: str = typer.Option("name", help="CSV column holding the names."),
    threshold: float = typer.Option(csl.DEFAULT_THRESHOLD, "--threshold", "-t"),
):
    """Screen names against 11 US sanctions and export-control lists (offline)."""
    c = ctx()
    if not csl.available(c):
        die("CSL not synced. Run: govint sync csl")
    if file:
        with file.open(newline="") as f:
            rows = list(csv.DictReader(f))
        if rows and column not in rows[0]:
            die(f"column {column!r} not in CSV (have: {', '.join(rows[0])})")
        w = csv.writer(sys.stdout)
        w.writerow(["query", "score", "matched_name", "list", "programs", "country", "id"])
        for r in rows:
            hits = csl.screen(c, r[column], threshold)
            if not hits:
                w.writerow([r[column], "", "", "", "", "", ""])
            for h in hits:
                rec = h.record
                w.writerow([r[column], f"{h.score:.1f}", rec["name"], rec["source"],
                            ";".join(rec.get("programs") or []), rec.get("country") or "", rec["id"]])
        return
    if not name:
        die("give a name or --csv FILE")
    hits = csl.screen(c, name, threshold)
    if STATE["json"]:
        return emit_json([{"score": h.score, "matched": h.matched, **h.record} for h in hits])
    if not hits:
        out.print(f"[green]No matches[/] for {name!r} at threshold {threshold:g}.")
        return
    t = Table(title=f"Screening hits for {name!r}")
    for col in ("Score", "Name", "List", "Programs", "Country", "Type"):
        t.add_column(col)
    for h in hits:
        r = h.record
        color = "red" if h.score >= 95 else "yellow"
        shown = r["name"]
        if h.matched != names.normalize(r["name"]):
            shown += f"\n[dim]matched alias: {h.matched}[/]"
        t.add_row(f"[{color}]{h.score:.0f}[/]", shown, r["source"].split(" - ")[0],
                  ", ".join(r.get("programs") or []), r.get("country") or "", r.get("type") or "")
    out.print(t)


@app.command("edgar")
def edgar_cmd(
    query: str = typer.Argument(..., help="Company name, ticker, or CIK."),
    insiders: bool = typer.Option(True, "--insiders/--no-insiders"),
    filings: int = typer.Option(10, help="Recent filings to list."),
):
    """SEC company profile, recent filings, and insiders."""
    c = ctx()
    try:
        hits = edgar.search(c, query)
        if not hits:
            die(f"no EDGAR filer matches {query!r}")
        cik = hits[0]["cik"]
        r = edgar.profile(c, cik)
        sub = edgar.submissions(c, cik)
        people, notes = edgar.insiders(c, cik) if insiders else ([], [])
    except SourceError as e:
        die(str(e))
    node = r.nodes[0]
    if STATE["json"]:
        return emit_json({"profile": node.props | {"name": node.label},
                          "addresses": [n.label for n in r.nodes[1:]],
                          "insiders": [p.__dict__ | {"roles": sorted(p.roles)} for p in people]})
    if len(hits) > 1 and hits[1]["score"] >= 80:
        err.print("[dim]other matches: " + "; ".join(f"{h['name']} (CIK {h['cik']})" for h in hits[1:5]) + "[/]")
    p = node.props
    t = Table(title=f"{node.label}  ·  CIK {cik}", show_header=False)
    for k in ("tickers", "exchanges", "sic", "state_of_incorporation", "ein", "lei", "phone", "website",
              "entity_type", "former_names", "last_filing"):
        v = p.get(k)
        if v:
            t.add_row(k, ", ".join(v) if isinstance(v, list) else str(v))
    for a in r.nodes[1:]:
        t.add_row("address", a.label)
    t.add_row("source", node.sources[0]["url"])
    out.print(t)

    rec = sub["filings"]["recent"]
    ft = Table(title="Recent filings")
    for col in ("Date", "Form", "Description", "Link"):
        ft.add_column(col)
    for i in range(min(filings, len(rec["form"]))):
        acc = rec["accessionNumber"][i].replace("-", "")
        link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{rec['primaryDocument'][i].split('/')[-1]}"
        ft.add_row(rec["filingDate"][i], rec["form"][i], rec["primaryDocDescription"][i] or "", link)
    if filings:
        out.print(ft)

    if people:
        it = Table(title="Insiders (recent Forms 3/4/5)")
        for col in ("Name", "Role", "Last filing", "Open-mkt buys", "Open-mkt sells"):
            it.add_column(col)
        for pp in people:
            it.add_row(pp.name, pp.title or ", ".join(sorted(pp.roles)), pp.last_date, usd(pp.bought) if pp.bought else "",
                       usd(pp.sold) if pp.sold else "")
        out.print(it)
    for n in notes:
        err.print(f"[dim]{n}[/]")


@app.command()
def tail(
    ident: Optional[str] = typer.Argument(None, help="N-number or Mode S hex."),
    owner: Optional[str] = typer.Option(None, "--owner", help="Search by registered owner name."),
):
    """FAA aircraft registry: who owns a tail number, or what an owner flies."""
    c = ctx()
    if not faa.available(c):
        die("FAA registry not synced. Run: govint sync faa")
    if not owner and not ident:
        die("give an N-number or --owner")
    rows = faa.by_owner(c, owner) if owner else faa.lookup(c, ident)
    if STATE["json"]:
        return emit_json(rows)
    if not rows:
        out.print("No aircraft found.")
        return
    t = Table(title=f"FAA registry: {owner or ident}")
    for col in ("N-number", "Aircraft", "Owner", "Type", "Location", "Mode S", "Cert issued", "Expires"):
        t.add_column(col)
    for a in rows:
        t.add_row(a["n_number"], f"{a.get('year_mfr') or ''} {(a.get('mfr') or '').strip()} {(a.get('model') or '').strip()}".strip(),
                  a["name"], a.get("registrant_type") or "", f"{a['city']}, {a['state']}", a.get("mode_s_hex") or "",
                  a.get("cert_issue") or "", a.get("expiration") or "")
    out.print(t)
    if not owner and rows:
        csl_hits = csl.screen(c, rows[0]["name"]) if csl.available(c) else []
        for h in csl_hits:
            out.print(f"[red]⚑ owner resembles screening-list entry:[/] {h.record['name']} ({h.record['source']}, score {h.score:.0f})")


@app.command()
def spend(
    name: str,
    years: int = typer.Option(5, help="Look-back in years (0 = since FY2008)."),
    grants: bool = typer.Option(False, "--grants", help="Grants instead of contracts."),
    limit: int = typer.Option(25),
):
    """Federal contracts or grants paid to a recipient (USAspending)."""
    c = ctx()
    try:
        recs = usaspending.recipients(c, name)
        data = usaspending.awards(c, name, years=years, kinds=usaspending.GRANTS if grants else usaspending.CONTRACTS,
                                  limit=limit)
    except SourceError as e:
        die(str(e))
    if STATE["json"]:
        return emit_json({"recipients": recs, "awards": data.get("results", [])})
    if recs:
        rt = Table(title="Matching recipient records")
        for col in ("Name", "UEI", "Level", "Lifetime $"):
            rt.add_column(col)
        for r in recs[:10]:
            rt.add_row(r["name"], r.get("uei") or "", {"P": "parent", "C": "child", "R": "recipient"}.get(r.get("recipient_level"), ""),
                       usd(r.get("amount")))
        out.print(rt)
    t = Table(title=f"Top {'grants' if grants else 'contracts'}, last {years or 'all'} years")
    for col in ("Award", "Amount", "Recipient", "Agency", "Start", "Description"):
        t.add_column(col, overflow="fold")
    for a in data.get("results", []):
        t.add_row(a["Award ID"], usd(a["Award Amount"]), a["Recipient Name"], a["Awarding Sub Agency"] or a["Awarding Agency"],
                  a["Start Date"] or "", (a["Description"] or "")[:70])
    out.print(t)
    by = usaspending.summarize_by_agency(data)
    if by:
        out.print("[dim]by agency (top results only): " + "; ".join(f"{k} {usd(v)}" for k, v, _ in by[:5]) + "[/]")


@app.command("fec")
def fec_cmd(
    name: str,
    person: bool = typer.Option(False, "--person", help="Treat NAME as an individual donor."),
    cycle: Optional[int] = typer.Option(None, help="Two-year cycle, e.g. 2026."),
):
    """Political money: an org's PACs and employee giving, or a person's donations."""
    c = ctx()
    try:
        if person:
            rows = fec.contributions_by_person(c, name)
            if STATE["json"]:
                return emit_json(rows)
            t = Table(title=f"Itemized contributions: {name}")
            for col in ("Date", "Amount", "Contributor", "Employer / occupation", "Location", "Recipient"):
                t.add_column(col, overflow="fold")
            for r in rows:
                t.add_row(r.get("contribution_receipt_date", "")[:10], usd(r.get("contribution_receipt_amount")),
                          r.get("contributor_name") or "",
                          f"{r.get('contributor_employer') or ''} / {r.get('contributor_occupation') or ''}",
                          f"{r.get('contributor_city') or ''}, {r.get('contributor_state') or ''}",
                          (r.get("committee") or {}).get("name") or "")
            out.print(t)
            return
        cyc = cycle or fec.cycle_now()
        pacs = fec.committees(c, name)
        giving = fec.employer_giving(c, name, cyc)
        labels = fec.committee_names(c, [g["committee_id"] for g in giving[:15]])
        pac_money = {p["id"]: fec.pac_recipients(c, p["id"], cyc, pages=1)[:10] for p in pacs[:3] if p.get("is_active")}
    except SourceError as e:
        die(str(e))
    if STATE["json"]:
        return emit_json({"pacs": pacs, "pac_recipients": pac_money, "employee_giving": giving})
    for p in pacs:
        out.print(f"[b]PAC[/] {p['name']} ({p['id']}, {'active' if p.get('is_active') else 'inactive'})  {fec.committee_url(p['id'])}")
        for r in pac_money.get(p["id"], []):
            out.print(f"    {usd(r['total']):>12}  → {r['name']}")
    if giving:
        t = Table(title=f"Individual donations listing employer ~{name!r}, {cyc} cycle")
        for col in ("Recipient committee", "Total", "Donations", "Employer string"):
            t.add_column(col)
        for g in giving[:15]:
            t.add_row(labels.get(g["committee_id"], g["committee_id"]), usd(g["total"]), str(g["count"]), g["employer"])
        out.print(t)
    if not pacs and not giving:
        out.print("No FEC activity found.")


@app.command()
def lei(name: str, children: int = typer.Option(25, help="Max subsidiaries to list.")):
    """Legal Entity Identifier lookup with parent and subsidiary chain (GLEIF)."""
    c = ctx()
    try:
        recs = gleif.search(c, name)
        if not recs:
            die(f"no LEI record matches {name!r}")
        r = gleif.tree_result(c, recs[0], children)
    except SourceError as e:
        die(str(e))
    me = r.nodes[0]
    if STATE["json"]:
        return emit_json({"entity": me.props | {"name": me.label},
                          "edges": [{"from": e.src, "rel": e.rel, "to": e.dst} for e in r.edges],
                          "nodes": {n.id: n.label for n in r.nodes}})
    labels = {n.id: n for n in r.nodes}
    root = Tree(f"[b]{me.label}[/]  LEI {me.props['lei']}  ({me.props.get('jurisdiction')}, {me.props.get('status')})")
    for e in r.edges:
        if e.src == me.id and e.rel in ("child_of", "ultimately_owned_by"):
            p = labels[e.dst]
            root.add(f"[cyan]{'ultimate parent' if e.rel == 'ultimately_owned_by' else 'parent'}[/] {p.label} ({p.props.get('jurisdiction')})")
    kids = [labels[e.src] for e in r.edges if e.dst == me.id and e.rel == "child_of"]
    if kids:
        sub = root.add(f"[green]{len(kids)} direct subsidiaries[/]")
        for k in kids:
            sub.add(f"{k.label} ({k.props.get('jurisdiction')}) {k.props['lei']}")
    out.print(root)
    if len(recs) > 1:
        err.print("[dim]other matches: " + "; ".join(x["attributes"]["entity"]["legalName"]["name"] for x in recs[1:6]) + "[/]")


@app.command("sam")
def sam_cmd(name: str):
    """SAM.gov registration and exclusion (debarment) check. Needs GOVINT_SAM_KEY."""
    c = ctx()
    if not c.settings.sam_key:
        die(sam.NO_KEY)
    try:
        ents, excl = sam.entities(c, name), sam.exclusions(c, name)
    except SourceError as e:
        die(str(e))
    if STATE["json"]:
        return emit_json({"entities": ents, "exclusions": excl})
    for e in ents:
        reg = e.get("entityRegistration") or {}
        out.print(f"[b]{reg.get('legalBusinessName')}[/] UEI {reg.get('ueiSAM')} CAGE {reg.get('cageCode')} "
                  f"status {reg.get('registrationStatus')} expires {reg.get('registrationExpirationDate')}")
    for x in excl:
        d = x.get("exclusionDetails") or {}
        out.print(f"[red]EXCLUSION[/] {x['_label']}: {d.get('exclusionType')} by {d.get('excludingAgencyName')}")
    if not ents and not excl:
        out.print("No SAM.gov records found.")


# ---------------------------------------------------------------- pivot / brief


def _do_pivot(name: str, depth: int) -> tuple[Graph, Node]:
    c = ctx()
    if not c.settings.contact:
        err.print("[yellow]SEC contact email not set; EDGAR will be skipped. "
                  "Run: govint config contact you@example.com[/]")
    with err.status("pivoting…") as st:
        return run_pivot(c, name, depth=depth, progress=lambda m: st.update(f"[dim]{m}[/]"))


def _tree(g: Graph, seed: Node) -> Tree:
    root = Tree(f"[b]{seed.label}[/]")
    seen = {seed.id}

    def label(n: Node) -> str:
        flag = f" [red]⚑ {', '.join(n.flags)}[/]" if n.flags else ""
        return f"[dim]{n.kind}[/] {n.label}{flag}"

    def walk(node: Node, branch: Tree, level: int) -> None:
        groups: dict[str, list] = {}
        for e, n in g.neighbors(node.id):
            if n.id in seen:
                continue
            groups.setdefault(e.rel, []).append((e, n))
        for rel, items in sorted(groups.items()):
            rb = branch.add(f"[cyan]{rel}[/] ({len(items)})")
            for e, n in items[:15]:
                seen.add(n.id)
                extra = ""
                if "usd" in e.props:
                    extra = f" [green]{usd(e.props['usd'])}[/]"
                elif "amount" in e.props:
                    extra = f" [green]{usd(e.props['amount'])}[/]"
                elif "role" in e.props:
                    extra = f" [dim]{e.props['role']}[/]"
                elif "score" in e.props:
                    extra = f" [dim]score {e.props['score']}[/]"
                nb = rb.add(label(n) + extra)
                if level < 2 and rel == "same_as":
                    walk(n, nb, level + 1)
            if len(items) > 15:
                rb.add(f"[dim]… {len(items) - 15} more[/]")

    walk(seed, root, 0)
    return root


@app.command()
def pivot(
    name: str,
    depth: int = typer.Option(1, min=1, max=2, help="2 = also expand related companies."),
    json_out: Optional[Path] = typer.Option(None, "--json-out", help="Write the full graph as JSON."),
    dot_out: Optional[Path] = typer.Option(None, "--dot", help="Write a Graphviz .dot file."),
):
    """Fan a name out across every source and show the linked graph."""
    g, seed = _do_pivot(name, depth)
    if json_out:
        json_out.write_text(g.to_json())
    if dot_out:
        dot_out.write_text(g.to_dot())
    if STATE["json"]:
        sys.stdout.write(g.to_json() + "\n")
        return
    out.print(_tree(g, seed))
    for n in g.notes:
        err.print(f"[yellow]note:[/] {n}")
    out.print(f"[dim]{len(g.nodes)} nodes, {len(g.edges)} edges[/]")


@app.command("brief")
def brief_cmd(
    name: str,
    output: Optional[Path] = typer.Option(None, "-o", "--output", help="Write markdown to a file."),
    depth: int = typer.Option(1, min=1, max=2),
):
    """One-command sourced dossier (markdown)."""
    g, seed = _do_pivot(name, depth)
    md = brief.render(g, seed)
    if output:
        output.write_text(md)
        err.print(f"wrote {output}")
    else:
        out.print(Markdown(md))


def main() -> None:
    app()

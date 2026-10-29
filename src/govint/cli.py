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


CONFIG_KEYS = {"mirror": "Base URL for prebuilt dataset mirrors (GitHub releases)",
               "contact": "Email for the SEC User-Agent (required by SEC)",
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
                n, where = faa.sync(c, file)
                out.print(f"FAA: {n:,} aircraft from {where}")
            except SourceError as e:
                err.print(f"[red]FAA failed:[/] {e}")
                failed = True
    if dataset not in ("csl", "faa", "all"):
        die("dataset must be csl, faa, or all")
    if failed:
        raise typer.Exit(1)


@app.command()
def mirror(
    dataset: str = typer.Argument("faa", help="Only faa for now."),
    file: Optional[Path] = typer.Option(None, "--file", help="FAA zip to build from (default: download from faa.gov)."),
    out_dir: Path = typer.Option(Path("mirror-out"), "--out", help="Where to write the build."),
    publish: Optional[str] = typer.Option(None, "--publish", help="GitHub repo (owner/name) to upload the release to."),
):
    """Build (and optionally publish) a prebuilt dataset for `govint sync` to fall back on."""
    import subprocess
    import tempfile

    from .sources import faa as faa_src
    if dataset != "faa":
        die("only 'faa' can be mirrored")
    c = ctx()
    if file is None:
        file = Path(tempfile.mkdtemp()) / "ReleasableAircraft.zip"
        with err.status("downloading from faa.gov…"):
            try:
                c.http.download(faa_src.URL, file)
            except SourceError as e:
                die(f"faa.gov download failed: {e}. Download it in a browser and pass --file.")
    with err.status("building mirror…"):
        m = faa_src.build_mirror(file, out_dir)
    out.print(f"built {out_dir / m['file']}: {m['rows']:,} aircraft, data dated {m['data_date']}, "
              f"{m['bytes'] / 1e6:.1f} MB, sha256 {m['sha256'][:12]}…")
    if not publish:
        return
    tag = faa_src.MIRROR_TAG
    files = [str(out_dir / m["file"]), str(out_dir / "faa.manifest.json")]
    notes = (f"FAA aircraft registry, data dated {m['data_date']} ({m['rows']:,} aircraft), prebuilt for "
             f"`govint sync faa`. Source: {faa_src.URL} (public domain, US government work).")
    exists = subprocess.run(["gh", "release", "view", tag, "--repo", publish], capture_output=True).returncode == 0
    if exists:
        cmds = [["gh", "release", "upload", tag, *files, "--clobber", "--repo", publish],
                ["gh", "release", "edit", tag, "--notes", notes, "--title", f"FAA registry {m['data_date']}",
                 "--repo", publish]]
    else:
        cmds = [["gh", "release", "create", tag, *files, "--title", f"FAA registry {m['data_date']}",
                 "--notes", notes, "--repo", publish]]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            die(f"{' '.join(cmd[:3])} failed: {r.stderr.strip()}")
    out.print(f"published to https://github.com/{publish}/releases/tag/{tag}")


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
        m = faa.meta(c)
        t.add_row("FAA registry", f"{n:,} aircraft, data dated {m.get('data_date', '?')} (from {m.get('source', '?')})")
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
            if rel.startswith("congress_trade:"):
                continue
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


# ---------------------------------------------------------------- congress

congress_app = typer.Typer(help="Congressional stock trades vs. committee seats and federal contracts.",
                           no_args_is_help=True)
app.add_typer(congress_app, name="congress")


def _since(days: int | None) -> str | None:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days)).isoformat() if days else None


@congress_app.command("sync")
def congress_sync(years: list[int] = typer.Argument(None, help="Years to sync (default: this year and last).")):
    """Download and parse House periodic transaction reports (PTRs)."""
    from datetime import date
    from rich.progress import Progress

    from .sources import congress
    c = ctx()
    years = years or [date.today().year - 1, date.today().year]
    for y in years:
        with Progress(console=err, transient=True) as prog:
            task = prog.add_task(f"House PTRs {y}", total=None)
            try:
                stats = congress.sync_house(c, y, lambda d, t: prog.update(task, completed=d, total=t))
            except SourceError as e:
                die(str(e))
        out.print(f"{y}: {stats['filings']} PTR filings, {stats['new']} new, {stats['trades']} trades parsed, "
                  f"{stats['scanned']} scanned paper filings skipped, {stats['errors']} errors")


@congress_app.command("trades")
def congress_trades(
    member: Optional[str] = typer.Option(None, "--member", "-m"),
    ticker: Optional[str] = typer.Option(None, "--ticker", "-t"),
    days: Optional[int] = typer.Option(None, "--days", help="Only trades in the last N days."),
    limit: int = typer.Option(50),
):
    """List disclosed trades."""
    from .sources import congress
    c = ctx()
    if not congress.available(c):
        die("no trades synced. Run: govint congress sync")
    rows = congress.trades(c, member, ticker, _since(days), limit)
    if STATE["json"]:
        return emit_json(rows)
    t = Table(title="Congressional trades (House PTRs)")
    for col in ("Trade date", "Filed", "Member", "Owner", "Type", "Ticker", "Asset", "Amount"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(r["date"], r["filed"], f"{r['member']} ({r['state_dst']})", r["owner"], r["type"],
                  r["ticker"] or "", r["asset"][:50], r["amount"])
    out.print(t)


@congress_app.command("member")
def congress_member(name: str):
    """A member's committees and disclosed trading."""
    from collections import Counter

    from .sources import congress
    c = ctx()
    ms = congress.members(c)
    rows = congress.trades(c, member=name, limit=10_000) if congress.available(c) else []
    m = congress.find_member(ms, name, rows[0]["state_dst"] if rows else None)
    if not m and not rows:
        die(f"no member or trades match {name!r}")
    if STATE["json"]:
        return emit_json({"member": m.__dict__ if m else None, "trades": rows})
    if m:
        out.print(f"[b]{m.name}[/] ({m.party}, {m.state}{'-' + str(m.district) if m.district else ''}, {m.chamber})")
        for cm in m.committees:
            secs = congress.committee_sectors(cm["name"])
            out.print(f"  {cm['name']}" + (f" [b]{cm['title']}[/]" if cm.get("title") else "")
                      + (f" [dim]→ {', '.join(secs)}[/]" if secs else ""))
    if rows:
        tick = Counter(r["ticker"] for r in rows if r["ticker"])
        lag = [r for r in rows if (_d(r["filed"]) - _d(r["date"])).days > congress.STOCK_ACT_DAYS]
        out.print(f"\n{len(rows)} disclosed trades, {len(lag)} filed late. Most traded: "
                  + ", ".join(f"{k} ({v})" for k, v in tick.most_common(10)))


def _d(s: str):
    from datetime import date
    return date.fromisoformat(s)


@congress_app.command("flags")
def congress_flags(
    days: int = typer.Option(120, "--days", help="Trades in the last N days."),
    member: Optional[str] = typer.Option(None, "--member", "-m"),
    ticker: Optional[str] = typer.Option(None, "--ticker", "-t"),
    contracts: bool = typer.Option(False, "--contracts", help="Also check federal contracts near each trade (slower)."),
    min_score: int = typer.Option(2, help="Hide trades scoring below this."),
    per_member: int = typer.Option(3, help="Max rows per member (0 = no cap)."),
    limit: int = typer.Option(40),
):
    """Rank trades by committee overlap, late filing, size, and nearby federal contracts."""
    from .sources import congress
    c = ctx()
    if not congress.available(c):
        die("no trades synced. Run: govint congress sync")
    rows = congress.trades(c, member, ticker, _since(days), limit=5000)
    rows = [r for r in rows if r["ticker"] or not contracts]
    with err.status("scoring trades…") as st:
        try:
            flagged = congress.flag_trades(c, rows, with_contracts=contracts,
                                           progress=lambda m: st.update(f"[dim]{m}[/]"))
        except SourceError as e:
            die(str(e))
    flagged = [f for f in flagged if f.score >= min_score]
    if per_member:
        from collections import Counter
        seen: Counter = Counter()
        capped = []
        for f in flagged:
            seen[f.trade["member"]] += 1
            if seen[f.trade["member"]] <= per_member:
                capped.append(f)
        flagged = capped
    flagged = flagged[:limit]
    if STATE["json"]:
        return emit_json([{"score": f.score, "reasons": f.reasons, "company": f.company, "sic": f.sic,
                           "member": f.member.name if f.member else None, "trade": f.trade,
                           "contracts": f.contracts} for f in flagged])
    if not flagged:
        out.print("Nothing scored above the threshold.")
        return
    t = Table(title=f"Flagged congressional trades, last {days} days")
    for col in ("Score", "Date", "Member", "Trade", "Why"):
        t.add_column(col, overflow="fold")
    for f in flagged:
        tr = f.trade
        who = f.member.name if f.member else tr["member"]
        t.add_row(str(f.score), tr["date"], f"{who} ({tr['state_dst']})",
                  f"{tr['type']} {tr['ticker'] or tr['asset'][:30]} {tr['amount']}" + (" [dim](" + tr["owner"] + ")[/]" if tr["owner"] != "self" else ""),
                  "\n".join(f.reasons))
    out.print(t)
    out.print("[dim]Signals, not accusations: committee overlap is by industry code, and filings are "
              "self-reported ranges. Check the linked PTR before drawing conclusions.[/]")


# ---------------------------------------------------------------- leaks


@app.command()
def leaks(
    companies: list[str] = typer.Argument(..., help="One or more company names."),
    days: int = typer.Option(90, "--days", help="Look-back window."),
    show_all: bool = typer.Option(False, "--all", help="Include routine paperwork (manufacturing notices, labeling)."),
    new: bool = typer.Option(False, "--new", help="Only items not seen on a previous --new run (for cron)."),
    fcc_grantee: Optional[str] = typer.Option(None, "--fcc-grantee", help="FCC grantee code, e.g. BCG for Apple."),
    fcc_id: Optional[str] = typer.Option(None, "--fcc-id", help="Specific FCC ID to open."),
):
    """Unreleased-product signals from FDA filings (+ FCC links to open in a browser)."""
    from .sources import leaks as lk
    c = ctx()
    items, notes = [], []
    with err.status("scanning filings…") as st:
        for co in companies:
            st.update(f"[dim]{co}[/]")
            i, n = lk.scan(c, co, days)
            items += i
            notes += n
    if not show_all:
        items = [i for i in items if i.signal]
    if new:
        items = lk.only_new(c, items)
    items.sort(key=lambda i: i.date, reverse=True)
    links = lk.fcc_links(fcc_grantee, fcc_id)
    if STATE["json"]:
        return emit_json({"items": [i.__dict__ for i in items], "fcc_links": links, "notes": notes})
    if items:
        t = Table(title=f"Regulatory filings, last {days} days" + (" (new only)" if new else ""))
        for col in ("Date", "Source", "Company", "Product", "Detail", "Link"):
            t.add_column(col, overflow="fold")
        for i in items:
            t.add_row(i.date, i.source, i.company, i.title, i.detail[:90], i.url)
        out.print(t)
    else:
        out.print("No new filings." if new else "No filings in the window.")
    for link in links:
        out.print(f"FCC (open in a browser; the FCC blocks scripted access): {link}")
    for n in notes:
        err.print(f"[yellow]note:[/] {n}")


# ---------------------------------------------------------------- sky

sky_app = typer.Typer(help="Live aircraft joined to the FAA registry.", no_args_is_help=True)
app.add_typer(sky_app, name="sky")


def _planes(c, where: Optional[str], radius: float, feed: Optional[str]):
    from .sources import sky
    center = sky.place(c, where) if where else None
    if feed:
        planes = sky.local_feed(c, feed)
    elif center:
        planes = sky.opensky(c, sky.bbox(*center, radius))
    else:
        die("give a place (address or 'lat,lon') or --feed URL")
    if center:
        for p in planes:
            p.dist = sky.distance_km(center[0], center[1], p.lat, p.lon) if p.lat is not None else None
        planes = [p for p in planes if p.dist is None or p.dist <= radius]
    return planes


@sky_app.command("now")
def sky_now(
    where: Optional[str] = typer.Argument(None, help="US street address or 'lat,lon'."),
    radius: float = typer.Option(25.0, "--radius", "-r", help="km"),
    feed: Optional[str] = typer.Option(None, "--feed", help="dump1090/readsb aircraft.json URL instead of OpenSky."),
    show_all: bool = typer.Option(False, "--all", help="Show every aircraft, not just flagged ones."),
):
    """Who's overhead: registered owner of every aircraft in range."""
    from .sources import faa as faa_src
    from .sources import sky
    c = ctx()
    try:
        planes = sky.annotate(c, _planes(c, where, radius, feed))
    except SourceError as e:
        die(str(e))
    if not faa_src.available(c):
        err.print("[yellow]FAA registry not synced, so owners are unknown. Run: govint sync faa --file <zip>[/]")
    shown = planes if show_all else [p for p in planes if p.flags]
    shown.sort(key=lambda p: (not p.flags, getattr(p, "dist", 0) or 0))
    if STATE["json"]:
        return emit_json([{**{k: v for k, v in p.__dict__.items() if k != "reg"}, "tail": p.tail, "owner": p.owner,
                           "model": p.model, "registry": p.reg} for p in shown])
    t = Table(title=f"{len(planes)} aircraft in range, {len(shown)} shown" + ("" if show_all else " (flagged only; --all for everything)"))
    for col in ("Hex", "Callsign", "Tail", "Aircraft", "Owner", "Alt ft", "kt", "km", "Flags"):
        t.add_column(col, overflow="fold")
    for p in shown:
        d = getattr(p, "dist", None)
        t.add_row(p.hex, p.callsign, p.tail, p.model, p.owner, "ground" if p.on_ground else str(p.alt_ft or ""),
                  str(p.speed_kt or ""), f"{d:.0f}" if d is not None else "", f"[red]{', '.join(p.flags)}[/]" if p.flags else "")
    out.print(t)


@sky_app.command("watch")
def sky_watch(
    owner: list[str] = typer.Option(None, "--owner", "-o", help="Registered owner to watch (repeatable)."),
    tail: list[str] = typer.Option(None, "--tail", help="N-number or hex to watch (repeatable)."),
    interval: int = typer.Option(120, help="Seconds between checks (OpenSky anonymous limits are tight)."),
    once: bool = typer.Option(False, "--once", help="Check once and exit (for cron)."),
):
    """Alert when a watched owner's aircraft is airborne."""
    import time

    from .sources import faa as faa_src
    from .sources import sky
    c = ctx()
    if not faa_src.available(c):
        die("FAA registry not synced. Run: govint sync faa --file <zip>")
    fleet: dict[str, dict] = {}
    for o in owner or []:
        for r in sky.owner_hexes(c, o):
            fleet[r["mode_s_hex"].upper()] = r
    for tl in tail or []:
        for r in faa_src.lookup(c, tl):
            if r.get("mode_s_hex"):
                fleet[r["mode_s_hex"].upper()] = r
    if not fleet:
        die("no aircraft with Mode S codes match those owners/tails")
    err.print(f"watching {len(fleet)} aircraft: " + ", ".join(f"{r['n_number']} ({r['name']})" for r in list(fleet.values())[:10]))
    airborne: set[str] = set()
    while True:
        try:
            planes = sky.opensky(c, icao24=list(fleet))
        except SourceError as e:
            err.print(f"[yellow]{e}[/]")
            planes = []
        now = {p.hex for p in planes if not p.on_ground}
        for p in planes:
            if p.hex in now and p.hex not in airborne:
                r = fleet[p.hex]
                out.print(f"[green]AIRBORNE[/] {r['n_number']} ({r['name']}) callsign {p.callsign or '-'} at "
                          f"{p.lat:.3f},{p.lon:.3f} {p.alt_ft or '?'} ft heading {p.heading}°  "
                          f"https://globe.adsbexchange.com/?icao={p.hex.lower()}")
        for h in airborne - now:
            out.print(f"[dim]LANDED/LOST[/] {fleet[h]['n_number']} ({fleet[h]['name']})")
        airborne = now
        if once:
            if not now:
                out.print("none airborne")
            return
        time.sleep(interval)

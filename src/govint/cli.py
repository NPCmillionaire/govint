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
    http = Http(s.user_agent, s.cache_path, use_cache=STATE["cache"])
    # lda.gov allows 15 requests a minute anonymously, 120 with a key.
    http.spacing["lda.gov"] = 0.5 if s.lda_key else 4.0
    return Ctx(settings=s, http=http)


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
               "ntfy": "ntfy topic or URL for `govint watch run` alerts (https://ntfy.sh)",
               "contact": "Email for the SEC User-Agent (required by SEC)",
               "data_gov_key": "api.data.gov key for FEC (https://api.data.gov/signup/)",
               "sam_key": "SAM.gov API key (sam.gov > Account Details)",
               "courtlistener_token": "CourtListener API token (https://www.courtlistener.com/profile/api/)",
               "lda_key": "lda.gov lobbying API key (https://lda.gov/api/register/), raises 15 -> 120 req/min"}


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
    dataset: str = typer.Argument("all", help="csl, faa, fara, offshore, ppp, or all"),
    file: Optional[Path] = typer.Option(None, "--file", help="Use an already-downloaded FAA zip."),
    full: bool = typer.Option(False, "--all-loans", help="PPP: load all 11.5M loans (~5 GB), not just $150k+."),
):
    """Download bulk datasets for offline use (sanctions list, FAA registry, FARA)."""
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
    if dataset in ("fara", "all"):
        from .sources import fara as fara_src
        with err.status("downloading FARA registrations…"):
            try:
                n = fara_src.sync(c)
                out.print(f"FARA: {n['fara_registrants']:,} registrants, {n['fara_principals']:,} foreign "
                          f"principals, {n['fara_agents']:,} individual agents")
            except SourceError as e:
                err.print(f"[red]FARA failed:[/] {e}")
                failed = True
    if dataset in ("offshore", "all"):
        from .sources import offshore
        with err.status("ICIJ Offshore Leaks…") as st:
            try:
                n = offshore.sync(c, progress=lambda m: st.update(f"ICIJ: {m}…"))
                out.print(f"ICIJ: {n['entity']:,} offshore entities, {n['officer']:,} officers, "
                          f"{n['intermediary']:,} intermediaries, {n['relationships']:,} links")
            except SourceError as e:
                err.print(f"[red]ICIJ failed:[/] {e}")
                failed = True
    if dataset in ("ppp", "all"):
        from .sources import ppp as ppp_src
        with err.status("SBA PPP loans…") as st:
            try:
                n = ppp_src.sync(c, full=full, progress=lambda m: st.update(f"PPP: {m}…"))
                out.print(f"PPP: {n:,} loans" + ("" if full else " of $150k or more (--all-loans for the rest)"))
            except SourceError as e:
                err.print(f"[red]PPP failed:[/] {e}")
                failed = True
    if dataset not in ("csl", "faa", "fara", "offshore", "ppp", "all"):
        die("dataset must be csl, faa, fara, offshore, ppp, or all")
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
    from .sources import fara as fara_src, offshore
    if fara_src.available(c):
        n = c.db().execute("select count(*) from fara_principals").fetchone()[0]
        t.add_row("FARA", f"{n:,} foreign principals")
    else:
        t.add_row("FARA", "[yellow]not synced[/] (govint sync fara)")
    if offshore.available(c):
        t.add_row("ICIJ Offshore Leaks", f"data generated {offshore.meta(c).get('generated', '?')}, "
                                         f"loaded {_age(offshore.path(c))}")
    else:
        t.add_row("ICIJ Offshore Leaks", "[yellow]not synced[/] (govint sync offshore)")
    from .sources import ppp as ppp_src
    if ppp_src.available(c):
        m = ppp_src.meta(c)
        t.add_row("PPP loans", f"{int(m.get('loans', 0)):,} loans ({'all' if m.get('scope') == 'all' else '$150k+'}), "
                               f"SBA release {m.get('release')}")
    else:
        t.add_row("PPP loans", "[yellow]not synced[/] (govint sync ppp)")
    t.add_row("SEC contact", s.contact or "[red]unset[/] (govint config contact you@example.com)")
    t.add_row("FEC key", "DEMO_KEY" if s.data_gov_key == "DEMO_KEY" else "set")
    t.add_row("SAM key", "set" if s.sam_key else "[yellow]unset[/]")
    t.add_row("LDA key", "set" if s.lda_key else "unset (lobbying limited to 15 req/min)")
    t.add_row("CourtListener", "set" if s.courtlistener_token else "[yellow]unset[/] (court records skipped)")
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


# ---------------------------------------------------------------- watch

watch_app = typer.Typer(help="Watchlist: alert on new filings, contracts, trades, listings, flights.",
                        no_args_is_help=True)
app.add_typer(watch_app, name="watch")


@watch_app.command("add")
def watch_add(
    kind: str = typer.Argument(..., help="company, ticker, tail, owner, keyword (sanctions), phrase (SEC full text), lobbying, or court"),
    value: str = typer.Argument(...),
):
    """Add something to watch. Its first check records a silent baseline."""
    from . import watch as w
    try:
        it = w.add(ctx(), kind, value)
    except ValueError as e:
        die(str(e))
    out.print(f"watching #{it.id}: {it.kind} {it.value}")


@watch_app.command("ls")
def watch_ls():
    """List watched items."""
    from . import watch as w
    its = w.items(ctx())
    if STATE["json"]:
        return emit_json([i.__dict__ for i in its])
    if not its:
        out.print("Watchlist is empty. Try: govint watch add company \"Palantir Technologies\"")
        return
    t = Table()
    for col in ("#", "Kind", "Value", "Resolved as", "Baselined"):
        t.add_column(col)
    for i in its:
        t.add_row(str(i.id), i.kind, i.value, i.label if i.label != i.value else "", "yes" if i.baselined else "not yet")
    out.print(t)


@watch_app.command("rm")
def watch_rm(item_id: int):
    """Remove a watched item by number."""
    from . import watch as w
    if not w.remove(ctx(), item_id):
        die(f"no watch item #{item_id}")
    out.print(f"removed #{item_id}")


@watch_app.command("run")
def watch_run(
    notify: bool = typer.Option(True, "--notify/--no-notify", help="Send to the configured ntfy topic."),
    loop: Optional[int] = typer.Option(None, "--every", help="Repeat every N minutes instead of exiting."),
):
    """Check everything once (cron/systemd) or on a loop; print and push what's new."""
    import time

    from . import watch as w
    c = ctx()
    if not w.items(c):
        die("watchlist is empty; add something with `govint watch add`")
    while True:
        events, errors = w.run(c, log=lambda m: err.print(f"[dim]{m}[/]"))
        for e in events:
            out.print(f"[b]{e.source}[/] {e.title}" + (f"  {e.url}" if e.url else ""))
            if notify and c.settings.ntfy:
                try:
                    w.notify_ntfy(c, c.settings.ntfy, e)
                except Exception as ex:
                    err.print(f"[yellow]ntfy failed: {ex}[/]")
        for x in errors:
            err.print(f"[yellow]{x}[/]")
        if STATE["json"]:
            emit_json([{"source": e.source, "title": e.title, "url": e.url, "when": e.when,
                        "item": e.item.value} for e in events])
        elif not events:
            err.print("[dim]nothing new[/]")
        if not loop:
            return
        time.sleep(loop * 60)


@app.command("fara")
def fara_cmd(
    name: str,
    show_all: bool = typer.Option(False, "--all", help="Include terminated registrations."),
):
    """Foreign agents: who represents a country or company, or whom a firm or person represents."""
    from .sources import fara as fara_src
    c = ctx()
    if not fara_src.available(c):
        die("FARA not synced. Run: govint sync fara")
    countries = fara_src.by_country(c, name, active_only=not show_all)
    if countries:
        if STATE["json"]:
            return emit_json(countries)
        t = Table(title=f"FARA: {len(countries)} {'' if show_all else 'active '}foreign principals from {name.upper()}")
        for col in ("Foreign principal", "Represented by", "Since", "Until", "Reg #"):
            t.add_column(col, overflow="fold")
        for r in countries:
            t.add_row(r["principal"], r["registrant"], r["since"], r["until"] or "", r["reg"])
        out.print(t)
        return
    regs = fara_src.search(c, name)
    if not show_all:
        regs = [r for r in regs if r.active or r.matched_as == "principal"]
    if STATE["json"]:
        return emit_json([r.__dict__ | {"url": r.url} for r in regs])
    if not regs:
        out.print("No FARA registrations match." + ("" if show_all else " (--all includes terminated ones)"))
        return
    for r in regs:
        status = "[green]active[/]" if r.active else f"[dim]terminated {r.terminated}[/]"
        root = Tree(f"[b]{r.registrant}[/]  #{r.reg}  {status}  [dim]matched as {r.matched_as}, "
                    f"score {r.score:.0f}[/]  {r.url}")
        ps = [p for p in r.principals if show_all or not p["until"]]
        if ps:
            b = root.add(f"[cyan]represents {len(ps)} foreign principal(s)[/]")
            for p in ps[:20]:
                b.add(f"{p['principal']} [dim]({p['country']}, since {p['since']}"
                      + (f", until {p['until']}" if p["until"] else "") + ")[/]")
        ags = [a for a in r.agents if show_all or not a["until"]]
        if ags:
            b = root.add(f"[cyan]{len(ags)} registered individual agent(s)[/]")
            for a in ags[:15]:
                b.add(f"{a['first']} {a['last']} [dim](since {a['since']})[/]")
        out.print(root)


@app.command()
def near(
    where: str = typer.Argument(..., help="US street address or 'lat,lon'."),
    radius: float = typer.Option(1.0, "--radius", "-r", help="Miles, for EPA facilities."),
):
    """Everything public records say about a place."""
    from .sources import near as nr
    from .sources import sky
    c = ctx()
    try:
        lat, lon = sky.place(c, where)
    except SourceError as e:
        die(str(e))
    with err.status("checking sources…"):
        rep = nr.report(c, lat, lon, radius)
    if STATE["json"]:
        import dataclasses
        return emit_json(dataclasses.asdict(rep))
    p = rep.place
    dist = "at-large" if p.district == 0 else (f"district {p.district}" if p.district else "district ?")
    out.print(f"[b]{p.city or '?'}, {p.county} ({p.state})[/]  census tract {p.tract}, congressional {dist}  "
              f"[dim]{lat:.5f},{lon:.5f}[/]")
    if rep.alerts:
        for a in rep.alerts:
            out.print(f"[red]NWS {a['severity']}:[/] {a['headline'] or a['event']}")
    if rep.reps:
        t = Table(title="Represented by")
        for col in ("Member", "Chamber", "Party", "Committees", "Disclosed trades"):
            t.add_column(col, overflow="fold")
        for r in rep.reps:
            t.add_row(r["name"], r["chamber"], r["party"], "; ".join(r["committees"][:4]),
                      str(r["trades"]) if r["chamber"] == "house" else "[dim]n/a (Senate)[/]")
        out.print(t)
    if rep.facility_count and not rep.facilities:
        out.print(f"EPA: {rep.facility_count} regulated facilities within {radius:g} mi, none with recent violations or penalties.")
    if rep.facilities:
        t = Table(title=f"EPA: {rep.facility_count} regulated facilities within {radius:g} mi; "
                        f"{len(rep.facilities)} with violations or penalties")
        for col in ("Facility", "Address", "Industry", "Status", "Qtrs in violation", "Penalties", "Report"):
            t.add_column(col, overflow="fold")
        for f in rep.facilities[:12]:
            status = "[red]significant violator[/]" if f["significant_violator"] else (f["status"] or "")
            t.add_row(f["name"] + (" [dim](federal)[/]" if f["federal"] else ""), f["address"], f["industry"],
                      status, str(f["quarters_in_violation"]), usd(f["penalties"]) if f["penalties"] else "", f["url"])
        out.print(t)
    if rep.contracts:
        t = Table(title=f"Biggest federal contracts performed in {p.county} with activity in the last 12 months")
        for col in ("Recipient", "Lifetime value", "Agency", "What"):
            t.add_column(col, overflow="fold")
        for k in rep.contracts:
            t.add_row(k["recipient"], usd(k["amount"]), k["agency"], k["description"])
        out.print(t)
    if rep.aircraft_count:
        out.print(f"\n[b]{rep.aircraft_count:,} aircraft registered in {p.county}[/]"
                  + (". Biggest multi-engine / helicopter operators:" if rep.aircraft else ""))
        for a in rep.aircraft:
            tails = a["tails"].split()
            more = f" +{len(tails) - 4} more" if len(tails) > 4 else ""
            out.print(f"  {a['n']:>3}  {a['name']} ({a['city']})  [dim]{(a['models'] or '')[:40]}; "
                      f"{' '.join(tails[:4])}{more}[/]")
    if rep.quakes:
        out.print("\n[b]Strongest earthquakes within 100 km, last 10 years:[/] " + "; ".join(
            f"M{q['mag']} {q['when']} ({q['place']})" for q in rep.quakes[:3]))
    for n in rep.notes:
        err.print(f"[yellow]note:[/] {n}")


# ---------------------------------------------------------------- lobbying, offshore, full text, shells


@app.command()
def lobby(
    name: str = typer.Argument(..., help="Client company, or a lobbying firm with --firm."),
    firm: bool = typer.Option(False, "--firm", help="Treat NAME as a lobbying firm and list its clients."),
    years: int = typer.Option(2, help="Years back, counting this one."),
):
    """Federal lobbying: spend, firms, issues, agencies, and ex-officials lobbying (LDA)."""
    from .sources import lobby as lb
    c = ctx()
    with err.status("querying lda.gov…"):
        try:
            fs = lb.filings(c, client=None if firm else name, firm=name if firm else None, years=years,
                            max_pages=40 if firm else 12)
        except SourceError as e:
            die(str(e))
    s = lb.summarize(name, fs, firm_view=firm)
    if STATE["json"]:
        import dataclasses
        return emit_json(dataclasses.asdict(s))
    if not fs:
        out.print(f"No lobbying reports for {name!r} in the last {years} years.")
        return
    out.print(f"[b]{name}[/]: {s.filings} quarterly reports; " + ", ".join(
        f"{y}: {usd(v)}" for y, v in s.by_year.items()) + (" received from clients" if firm else " spent"))
    t = Table(title="Clients" if firm else "Who lobbies for them")
    for col in ("Client" if firm else "Firm", "Amount", "Latest"):
        t.add_column(col, overflow="fold")
    for p in s.parties[:25]:
        label = p["name"] + (" [dim](in-house)[/]" if p["self"] else "")
        t.add_row(label, usd(p["amount"]), p["last"])
    out.print(t)
    if s.issues:
        out.print("[b]Issues:[/] " + ", ".join(f"{k} ({v})" for k, v in s.issues))
    if s.agencies:
        out.print("[b]Contacted:[/] " + ", ".join(f"{k} ({v})" for k, v in s.agencies))
    if s.foreign:
        out.print("[red]Foreign entities with an interest:[/] " + ", ".join(
            f"{f['name']} ({f['country']})" for f in s.foreign))
    if s.revolving:
        t = Table(title=f"Revolving door: {len(s.revolving)} lobbyists who held government jobs")
        for col in ("Lobbyist", "Firm", "Former position"):
            t.add_column(col, overflow="fold")
        for r in s.revolving[:30]:
            t.add_row(r["name"], r["firm"], r["position"][:160])
        out.print(t)
    if s.descriptions:
        out.print("[b]What they lobbied on (sample):[/]")
        for d in s.descriptions[:8]:
            out.print(f"  - {d[:200]}")


@app.command()
def offshore(
    name: str = typer.Argument(..., help="Company, person, or intermediary name; or a node id."),
    threshold: float = typer.Option(90, help="Fuzzy match threshold."),
):
    """ICIJ Offshore Leaks: Panama, Paradise and Pandora Papers, and more."""
    from .sources import offshore as off
    c = ctx()
    if not off.available(c):
        die("ICIJ data not synced. Run: govint sync offshore")
    if name.isdigit():
        n = off.node(c, int(name))
        hits = [off.Hit(n, 100.0)] if n else []
    else:
        hits = off.search(c, name, threshold=threshold)
    for h in hits:
        h.links = off.links(c, h.node["id"])
    if STATE["json"]:
        return emit_json([{"node": h.node, "score": h.score, "url": h.url, "links": h.links} for h in hits])
    if not hits:
        out.print("No match in the Offshore Leaks database.")
        return
    for h in hits[:10]:
        n = h.node
        bits = [x for x in (n["jurisdiction"], n["countries"], n["status"],
                            f"incorporated {n['incorporated']}" if n["incorporated"] else "",
                            f"via {n['provider']}" if n["provider"] else "") if x]
        root = Tree(f"[b]{n['name']}[/] [dim]{n['kind']}, {n['source']}, score {h.score:.0f}[/]  {h.url}"
                    + (f"\n[dim]{'; '.join(bits)}[/]" if bits else ""))
        groups: dict[str, list] = {}
        for ln in h.links:
            label = ln["link"] or ln["rel"]
            groups.setdefault(f"{label} ({'this →' if ln['dir'] == 'out' else '→ this'})", []).append(ln)
        for label, lns in groups.items():
            b = root.add(f"[cyan]{label}[/]")
            for ln in lns[:12]:
                b.add(f"{ln['name']} [dim]#{ln['id']} {ln['kind']}{', ' + ln['countries'] if ln['countries'] else ''}[/]")
            if len(lns) > 12:
                b.add(f"[dim]+{len(lns) - 12} more[/]")
        out.print(root)
    out.print("[dim]Source: ICIJ Offshore Leaks Database (ODbL). Appearing in it doesn't imply wrongdoing.[/]")


@app.command("grep")
def grep_cmd(
    query: str = typer.Argument(..., help='Words or "exact phrase" to find inside SEC filings.'),
    forms: Optional[str] = typer.Option(None, "--forms", "-f", help="Comma list, e.g. 10-K,8-K,DEF 14A"),
    since: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    until: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    limit: int = typer.Option(40),
):
    """Full-text search of every SEC filing since 2001 (exhibits included)."""
    c = ctx()
    q = query if '"' in query or " " not in query else f'"{query}"'
    try:
        total, hits, top = edgar.fulltext(c, q, forms=forms.split(",") if forms else None, since=since,
                                          until=until, pages=max(1, (limit + 99) // 100))
    except SourceError as e:
        die(str(e))
    if STATE["json"]:
        return emit_json({"total": total, "hits": hits[:limit], "top_filers": top})
    out.print(f"[b]{total:,}[/] filing documents mention {q}")
    if top:
        out.print("[b]Most mentions:[/] " + ", ".join(f"{n} ({k})" for n, k in top[:8]))
    t = Table()
    for col in ("Filed", "Filer", "Form", "Document", "Link"):
        t.add_column(col, overflow="fold")
    for h in hits[:limit]:
        t.add_row(h["filed"], h["filer"], h["form"], f"{h['file_type']}: {h['description']}"[:70], h["url"])
    out.print(t)


@app.command()
def shells(
    query: Optional[str] = typer.Argument(None, help="Company/person name, or a street address."),
    top: bool = typer.Option(False, "--top", help="Rank FAA addresses by number of distinct owners."),
    state: Optional[str] = typer.Option(None, help="With --top: limit to a state."),
):
    """Who else is registered at the same address, and who shares officers."""
    from .sources import shells as sh
    c = ctx()
    if top:
        if not faa.available(c):
            die("FAA registry not synced. Run: govint sync faa")
        with err.status("ranking addresses…"):
            rows = sh.top_faa(c, state=state)
        if STATE["json"]:
            return emit_json(rows)
        t = Table(title="Aircraft registration addresses with the most distinct owners")
        for col in ("Address", "Owners", "Aircraft", "Sample"):
            t.add_column(col, overflow="fold")
        for r in rows:
            t.add_row(r["address"], str(r["owners"]), str(r["aircraft"]), "; ".join(r["sample"]))
        out.print(t)
        return
    if not query:
        die("give a name or address, or --top")
    with err.status("collecting addresses…"):
        rep = sh.report(c, query)
    if STATE["json"]:
        import dataclasses
        return emit_json([dataclasses.asdict(a) | {"ppp": [ln.row for ln in a.ppp], "registrants": a.registrants,
                                                    "mass": a.mass}
                          for a in rep.addresses] + [{"shared_officers": rep.shared_officers, "notes": rep.notes}])
    for a in rep.addresses:
        regs = a.registrants
        tag = f"  [red]mass-registration address ({len(regs)} registrants)[/]" if a.mass else ""
        root = Tree(f"[b]{a.text}[/]  [dim]found via {', '.join(a.found_via)}[/]{tag}")
        if a.faa:
            b = root.add(f"[cyan]FAA: {len(a.faa)} aircraft registered here[/]")
            byname: dict[str, list[str]] = {}
            for r in a.faa:
                byname.setdefault(r["name"].strip(), []).append(r["n_number"])
            for nm, tails in sorted(byname.items(), key=lambda kv: -len(kv[1]))[:20]:
                b.add(f"{nm} [dim]{' '.join(tails[:5])}{' …' if len(tails) > 5 else ''}[/]")
            if len(byname) > 20:
                b.add(f"[dim]+{len(byname) - 20} more owners[/]")
        if a.icij:
            b = root.add(f"[cyan]ICIJ: {len(a.icij)} offshore entities/officers registered here[/]")
            for r in a.icij[:20]:
                b.add(f"{r['name']} [dim]{r['kind']}, {r['source']}, #{r['id']}[/]")
            if len(a.icij) > 20:
                b.add(f"[dim]+{len(a.icij) - 20} more[/]")
        if a.ppp:
            b = root.add(f"[cyan]PPP: {len(a.ppp)} loans to borrowers here[/]")
            for ln in a.ppp[:20]:
                r = ln.row
                b.add(f"{r['borrower']} [dim]{usd(r['amount'])} on {r['approved']}, {int(r['jobs'] or 0)} jobs[/]"
                      + (f" [red]{'; '.join(ln.flags)}[/]" if ln.flags else ""))
            if len(a.ppp) > 20:
                b.add(f"[dim]+{len(a.ppp) - 20} more[/]")
        if not a.faa and not a.icij and not a.ppp:
            root.add("[dim]nothing else on file here[/]")
        out.print(root)
    if rep.shared_officers:
        t = Table(title="Officers of matched offshore entities who also run other companies (ICIJ)")
        for col in ("Officer", "Role", "Of", "Also officer of"):
            t.add_column(col, overflow="fold")
        for o in sorted(rep.shared_officers, key=lambda o: -o["also_count"])[:20]:
            more = f" +{o['also_count'] - 5} more" if o["also_count"] > 5 else ""
            t.add_row(o["officer"], o["role"], o["of"], "; ".join(o["also"][:5]) + more)
        out.print(t)
    for n in rep.notes:
        err.print(f"[yellow]note:[/] {n}")


# ---------------------------------------------------------------- PPP, courts


def _loan_table(title: str, loans) -> Table:
    t = Table(title=title)
    for col in ("Borrower", "Where", "Approved", "Amount", "Forgiven", "Jobs", "Lender", "Flags"):
        t.add_column(col, overflow="fold")
    for ln in loans:
        r = ln.row
        t.add_row(r["borrower"] + (f" [dim]({r['business_type']})[/]" if r["business_type"] else ""),
                  f"{r['address']}, {r['city']}, {r['state']} {(r['zip'] or '')[:5]}", r["approved"],
                  usd(r["amount"]), usd(r["forgiven"]) if r["forgiven"] else "[dim]-[/]",
                  str(int(r["jobs"])) if r["jobs"] else "[dim]0[/]", r["lender"],
                  "[red]" + "; ".join(ln.flags) + "[/]" if ln.flags else "")
    return t


@app.command("ppp")
def ppp_cmd(
    name: Optional[str] = typer.Argument(None, help="Borrower name, or a street address with ZIP."),
    state: Optional[str] = typer.Option(None, "--state", "-s"),
    zip_code: Optional[str] = typer.Option(None, "--zip"),
    lender: Optional[str] = typer.Option(None, "--lender"),
    flagged: bool = typer.Option(False, "--flagged", help="Only loans over the per-job payroll cap or with no jobs."),
    clusters: bool = typer.Option(False, "--clusters", help="Addresses that borrowed under many different names."),
    limit: int = typer.Option(30),
):
    """SBA Paycheck Protection Program loans: who got what, and what looks off."""
    from .sources import ppp as pp
    c = ctx()
    if not pp.available(c):
        die("PPP data not synced. Run: govint sync ppp")
    if clusters:
        rows = pp.clusters(c, state=state, limit=limit)
        if STATE["json"]:
            return emit_json(rows)
        t = Table(title="Addresses with PPP loans to the most differently named borrowers")
        for col in ("Address", "Borrowers", "Loans", "Total", "Sample"):
            t.add_column(col, overflow="fold")
        for r in rows:
            t.add_row(f"{r['address']}, {r['city']}, {r['state']} {(r['zip'] or '')[:5]}", str(r["borrowers"]),
                      str(r["loans"]), usd(r["total"]), "; ".join(r["sample"]))
        out.print(t)
        return
    if name and name.strip()[:1].isdigit():
        loans, title = pp.at_address(c, name, zip_code), f"PPP loans at {name}"
    elif name:
        loans, title = pp.search(c, name, state=state, limit=limit), f"PPP loans matching {name!r}"
    else:
        loans = pp.top(c, state=state, lender=lender, zip_code=zip_code, flagged=flagged, limit=limit)
        title = "Largest PPP loans" + (f" in {state.upper()}" if state else "") + (f" in {zip_code}" if zip_code else "") \
            + (f" from {lender}" if lender else "") + (" over the per-job cap or with no jobs" if flagged else "")
    if STATE["json"]:
        return emit_json([ln.row | {"score": ln.score, "flags": ln.flags} for ln in loans])
    if not loans:
        scope = pp.meta(c).get("scope")
        out.print("No PPP loans match." + (" Only $150k+ loans are loaded; `govint sync ppp --all-loans` adds the rest."
                                          if scope != "all" else ""))
        return
    out.print(_loan_table(title, loans))
    total = sum(ln.row["amount"] or 0 for ln in loans)
    out.print(f"{len(loans)} loans, {usd(total)}. Source: {pp.DATASET_URL}")


@app.command("court")
def court_cmd(
    name: str = typer.Argument(..., help="Party name (company or person)."),
    opinions: bool = typer.Option(False, "--opinions", help="Search written opinions instead of dockets."),
    since: Optional[str] = typer.Option(None, help="Filed on or after YYYY-MM-DD."),
    limit: int = typer.Option(40),
):
    """Federal lawsuits, prosecutions and bankruptcies (CourtListener / PACER RECAP)."""
    from .sources import courts as ct
    c = ctx()
    try:
        total, cases = ct.search(c, name, opinions=opinions, limit=limit, since=since)
    except SourceError as e:
        die(str(e))
    s = ct.summarize(total, cases)
    if STATE["json"]:
        import dataclasses
        return emit_json(dataclasses.asdict(s))
    if not cases:
        out.print(f"No federal {'opinions' if opinions else 'dockets'} with {name!r} as a party.")
        return
    out.print(f"[b]{name}[/]: {total:,} {'opinions' if opinions else 'dockets'} found, showing {len(cases)} "
              "where the name matches a party")
    if s.tagged:
        out.print("[red]" + ", ".join(f"{k}: {v}" for k, v in s.tagged.items()) + "[/]")
    if s.by_nature:
        out.print("[b]Case types:[/] " + ", ".join(f"{k} ({v})" for k, v in s.by_nature))
    t = Table()
    for col in ("Filed", "Case", "Court", "Docket", "Type", "Status", "Link"):
        t.add_column(col, overflow="fold")
    for cs in cases:
        tags = f" [red][{', '.join(cs.tags)}][/]" if cs.tags else ""
        t.add_row(cs.filed, cs.name + tags, cs.court, cs.docket, cs.nature or cs.cause,
                  f"closed {cs.terminated}" if cs.terminated else "open", cs.url)
    out.print(t)

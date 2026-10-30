"""Watchlist: check companies, tickers, aircraft and sanctions keywords for anything new.

Each check yields events keyed by a stable id. The first run for an item records everything
it finds as a baseline without alerting, so later runs only report what's actually new.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from .http import SourceError
from .sources import congress, courts, csl, edgar, faa, leaks, lobby, sky, usaspending
from .sources.base import Ctx

KINDS = ("company", "ticker", "tail", "owner", "keyword", "phrase", "lobbying", "court")
# High-volume SEC forms that drown out signal.
SKIP_FORMS = {"424B2", "424B3", "424B5", "FWP", "424B4", "424B7", "S-8 POS", "SC 13G/A", "13F-HR"}


@dataclass
class Item:
    id: int
    kind: str
    value: str
    label: str
    baselined: bool
    props: dict


@dataclass
class Event:
    item: Item
    source: str
    key: str
    title: str
    url: str = ""
    when: str = ""


def _init(ctx: Ctx) -> None:
    ctx.db().executescript("""
    create table if not exists watch_items (
        id integer primary key, kind text, value text, label text, added text,
        baselined int default 0, props text default '{}');
    create table if not exists watch_seen (
        item_id int, key text, first_seen text, primary key (item_id, key));
    create table if not exists watch_state (key text primary key, value text);
    """)


def add(ctx: Ctx, kind: str, value: str) -> Item:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    _init(ctx)
    value = value.strip()
    if kind in ("ticker", "tail"):
        value = value.upper()
    db = ctx.db()
    cur = db.execute("insert into watch_items (kind, value, label, added) values (?,?,?,?)",
                     (kind, value, value, date.today().isoformat()))
    db.commit()
    return get(ctx, cur.lastrowid)


def get(ctx: Ctx, item_id: int) -> Item:
    r = ctx.db().execute("select * from watch_items where id=?", (item_id,)).fetchone()
    return Item(r["id"], r["kind"], r["value"], r["label"], bool(r["baselined"]), json.loads(r["props"]))


def items(ctx: Ctx) -> list[Item]:
    _init(ctx)
    return [get(ctx, r[0]) for r in ctx.db().execute("select id from watch_items order by id")]


def remove(ctx: Ctx, item_id: int) -> bool:
    _init(ctx)
    db = ctx.db()
    n = db.execute("delete from watch_items where id=?", (item_id,)).rowcount
    db.execute("delete from watch_seen where item_id=?", (item_id,))
    db.commit()
    return n > 0


def _save_props(ctx: Ctx, item: Item) -> None:
    ctx.db().execute("update watch_items set props=?, label=? where id=?",
                     (json.dumps(item.props), item.label, item.id))


# ---------------------------------------------------------------- checks


def _company(ctx: Ctx, item: Item) -> list[Event]:
    out: list[Event] = []
    name = item.value
    # Resolve the SEC filer once and remember it.
    if "cik" not in item.props:
        hits = edgar.search(ctx, name)
        item.props["cik"] = hits[0]["cik"] if hits and hits[0]["score"] >= 90 else None
        if item.props["cik"]:
            item.label = re.sub(r"\s*\([A-Z0-9.\-, ]+\)$", "", hits[0]["name"])
        _save_props(ctx, item)
    if cik := item.props.get("cik"):
        rec = edgar.submissions(ctx, cik, ttl=1800)["filings"]["recent"]
        for i in range(min(40, len(rec["form"]))):
            form = rec["form"][i]
            if form in SKIP_FORMS:
                continue
            acc = rec["accessionNumber"][i]
            doc = rec["primaryDocument"][i].split("/")[-1]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
            desc = rec["primaryDocDescription"][i] or ""
            what = "insider trade (Form 4)" if form == "4" else (form if desc in ("", form) else f"{form}: {desc}")
            out.append(Event(item, "SEC", f"sec:{acc}", f"{item.label}: {what}", url, rec["filingDate"][i]))

    since = date.today() - timedelta(days=90)
    body = {
        "filters": {"recipient_search_text": [name], "award_type_codes": usaspending.CONTRACTS,
                    "time_period": [{"start_date": since.isoformat(), "end_date": date.today().isoformat()}]},
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Sub Agency", "Start Date",
                   "Description", "generated_internal_id"],
        "limit": 25, "sort": "Start Date", "order": "desc",
    }
    for a in ctx.http.post(f"{usaspending.API}/search/spending_by_award/", body, ttl=3600).get("results", []):
        if not usaspending._same_family(name, a["Recipient Name"] or ""):
            continue
        out.append(Event(item, "USAspending", f"award:{a['generated_internal_id']}",
                         f"{a['Recipient Name']}: ${a['Award Amount'] or 0:,.0f} contract from "
                         f"{a['Awarding Sub Agency']}: {(a['Description'] or '')[:80]}",
                         usaspending.award_url(a["generated_internal_id"]), a["Start Date"] or ""))

    found, _notes = leaks.scan(ctx, name, days=120)
    for f in found:
        if f.signal:
            out.append(Event(item, f.source, f"fda:{f.source}|{f.id}", f"{f.company}: {f.source} {f.title}",
                             f.url, f.date))

    for h in csl.screen(ctx, item.label if item.props.get("cik") else name):
        r = h.record
        out.append(Event(item, "Sanctions", f"csl:{r['id']}",
                         f"{name} resembles {r['name']} on {r['source'].split(' - ')[0]} (score {h.score:.0f})",
                         r.get("source_information_url") or "", r.get("start_date") or ""))
    return out


def _ticker(ctx: Ctx, item: Item) -> list[Event]:
    out = []
    for t in congress.trades(ctx, ticker=item.value, limit=500):
        key = f"ptr:{t['doc_id']}:{t['date']}:{t['type']}:{t['amount_min']}:{t['owner']}"
        lag = (date.fromisoformat(t["filed"]) - date.fromisoformat(t["date"])).days
        late = f" (filed {lag} days later)" if lag > congress.STOCK_ACT_DAYS else ""
        out.append(Event(item, "Congress", key,
                         f"Rep. {t['member']} ({t['state_dst']}): {t['type']} {item.value} {t['amount']} "
                         f"on {t['date']}{late}", t["url"], t["filed"]))
    return out


def _keyword(ctx: Ctx, item: Item) -> list[Event]:
    """New screening-list entries mentioning a keyword (name, alias, program, or country)."""
    if not csl.available(ctx):
        return []
    p = csl.path(ctx)
    records, _, _ = csl._load(str(p), p.stat().st_mtime)
    kw = item.value.lower()
    out = []
    for r in records:
        hay = " ".join([r["name"], *(r.get("alt_names") or []), *(r.get("programs") or []),
                        r.get("country") or ""]).lower()
        if kw in hay:
            out.append(Event(item, "Sanctions", f"csl:{r['id']}",
                             f"New listing matching '{item.value}': {r['name']} ({r['source'].split(' - ')[0]})",
                             r.get("source_information_url") or "", r.get("start_date") or ""))
    return out


def _phrase(ctx: Ctx, item: Item) -> list[Event]:
    """New SEC filings containing a phrase (full-text search, last 60 days)."""
    q = item.value if '"' in item.value or " " not in item.value else f'"{item.value}"'
    since = (date.today() - timedelta(days=60)).isoformat()
    _total, hits, _top = edgar.fulltext(ctx, q, since=since, ttl=1800)
    return [Event(item, "SEC full text", f"efts:{h['accession']}",
                  f"{h['filer']} {h['form']} mentions {q}: {h['file_type']} {h['description']}"[:200],
                  h["url"], h["filed"]) for h in hits]


def _lobbying(ctx: Ctx, item: Item) -> list[Event]:
    """New lobbying reports (LD-2) naming the company as a client."""
    out = []
    for f in lobby.filings(ctx, client=item.value, years=1, max_pages=4):
        amt = lobby.amount(f)
        issues = sorted({a.get("general_issue_code_display") or "" for a in f.get("lobbying_activities") or []} - {""})
        out.append(Event(item, "Lobbying", f"lda:{f['filing_uuid']}",
                         f"{f['client']['name']} lobbying via {f['registrant']['name']}, "
                         f"{f['filing_year']} {f['filing_period'].replace('_', ' ')}"
                         + (f": ${amt:,.0f}" if amt else "") + (f" on {', '.join(issues[:4])}" if issues else ""),
                         f["filing_document_url"], f["dt_posted"][:10]))
    return out


def _court(ctx: Ctx, item: Item) -> list[Event]:
    """New federal dockets naming the party."""
    _total, cases = courts.search(ctx, item.value, limit=40)
    return [Event(item, "Court", f"cl:{c.court_id}:{c.docket}",
                  f"{c.name} ({c.court} {c.docket}, filed {c.filed})" + (f" [{', '.join(c.tags)}]" if c.tags else ""),
                  c.url, c.filed) for c in cases]


def _fleet(ctx: Ctx, item: Item) -> dict[str, dict]:
    if not faa.available(ctx):
        return {}
    rows = faa.lookup(ctx, item.value) if item.kind == "tail" else sky.owner_hexes(ctx, item.value)
    return {r["mode_s_hex"].upper(): r for r in rows if r.get("mode_s_hex")}


def _aircraft(ctx: Ctx, aircraft_items: list[Item]) -> list[Event]:
    """One OpenSky call for every watched aircraft; alert on ground -> airborne transitions."""
    fleet: dict[str, tuple[Item, dict]] = {}
    for it in aircraft_items:
        for hexcode, row in _fleet(ctx, it).items():
            fleet[hexcode] = (it, row)
    if not fleet:
        return []
    db = ctx.db()
    planes = sky.opensky(ctx, icao24=list(fleet))
    up = {p.hex: p for p in planes if not p.on_ground}
    out = []
    for hexcode, (it, row) in fleet.items():
        was = db.execute("select value from watch_state where key=?", (f"air:{hexcode}",)).fetchone()
        if hexcode in up and not was:
            p = up[hexcode]
            stamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
            out.append(Event(it, "ADS-B", f"air:{hexcode}:{stamp}",
                             f"{row['n_number']} ({row['name']}) airborne: callsign {p.callsign or '-'}, "
                             f"{p.alt_ft or '?'} ft at {p.lat:.3f},{p.lon:.3f}",
                             f"https://globe.adsbexchange.com/?icao={hexcode.lower()}", stamp[:10]))
            db.execute("insert or replace into watch_state values (?,?)", (f"air:{hexcode}", "1"))
        elif hexcode not in up and was:
            db.execute("delete from watch_state where key=?", (f"air:{hexcode}",))
    db.commit()
    return out


# ---------------------------------------------------------------- run


def refresh(ctx: Ctx, kinds: set[str], log: Callable[[str], None]) -> None:
    """Keep the local datasets the checks depend on fresh."""
    if "ticker" in kinds:
        try:
            stats = congress.sync_house(ctx, date.today().year)
            if stats["new"]:
                log(f"congress: {stats['new']} new filings")
        except SourceError as e:
            log(f"congress sync failed: {e}")
    if kinds & {"company", "keyword"}:
        p = csl.path(ctx)
        if not p.exists() or time.time() - p.stat().st_mtime > 86400:
            try:
                csl.sync(ctx)
                log("sanctions list refreshed")
            except SourceError as e:
                log(f"sanctions refresh failed: {e}")


def run(ctx: Ctx, log: Callable[[str], None] = lambda _: None) -> tuple[list[Event], list[str]]:
    """One pass over the watchlist. Returns (new events, errors)."""
    its = items(ctx)
    refresh(ctx, {i.kind for i in its}, log)
    db = ctx.db()
    events: list[Event] = []
    errors: list[str] = []
    found: list[tuple[Item, list[Event]]] = []
    for it in its:
        if it.kind in ("tail", "owner"):
            continue
        fn = {"company": _company, "ticker": _ticker, "keyword": _keyword, "phrase": _phrase,
              "lobbying": _lobbying, "court": _court}[it.kind]
        try:
            found.append((it, fn(ctx, it)))
        except SourceError as e:
            errors.append(f"{it.kind} {it.value}: {e}")
    air = [i for i in its if i.kind in ("tail", "owner")]
    if air:
        try:
            evs = _aircraft(ctx, air)
            for it in air:
                found.append((it, [e for e in evs if e.item.id == it.id]))
        except SourceError as e:
            errors.append(f"aircraft: {e}")

    today = date.today().isoformat()
    for it, evs in found:
        fresh = []
        for e in evs:
            cur = db.execute("insert or ignore into watch_seen values (?,?,?)", (it.id, e.key, today))
            if cur.rowcount:
                fresh.append(e)
        if it.baselined or it.kind in ("tail", "owner"):
            events += fresh
        else:
            log(f"baseline for {it.kind} {it.label}: {len(evs)} existing items recorded")
            db.execute("update watch_items set baselined=1 where id=?", (it.id,))
    db.commit()
    return events, errors


def notify_ntfy(ctx: Ctx, topic: str, e: Event) -> None:
    url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
    headers = {"Title": f"govint: {e.source}", "Tags": "mag"}
    if e.url:
        headers["Click"] = e.url
    ctx.http.client.post(url, content=e.title.encode(), headers=headers, timeout=15)


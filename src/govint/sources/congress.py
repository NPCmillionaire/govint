"""Congressional stock trades (House PTRs) cross-checked against committees and federal contracts."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .. import names
from ..http import SourceError
from .base import Ctx

HOUSE_INDEX = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
MEMBERSHIP = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"
COMMITTEES = "https://unitedstates.github.io/congress-legislators/committees-current.json"
TICKERS = "https://www.sec.gov/files/company_tickers.json"

STOCK_ACT_DAYS = 45  # a trade must be reported within 45 days

# ---------------------------------------------------------------- PDF parsing

_ANCHOR = re.compile(
    r"(?P<type>S \(partial\)|P|S|E)\s+(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notif>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+|Spouse/DC Over \$[\d,]+|\$[\d,]+)"
)
_NOISE = re.compile(
    r"^(ID Owner Asset Transaction|Type|Date Notification|Date|Amount Cap\.|Gains >|\$200\?|"
    r"Filing ID #.*|\* For the complete list.*|Name: .*|Status: .*|State/District: .*|"
    r"Clerk of the House.*)$"
)
_TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")
_ATYPE = re.compile(r"\[([A-Z]{2})\]")
TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}


@dataclass
class Trade:
    owner: str  # self, SP (spouse), JT (joint), DC (dependent child)
    asset: str
    ticker: str | None
    asset_type: str | None
    type: str
    date: str  # ISO
    notified: str  # ISO
    amount: str
    amount_min: int
    amount_max: int | None


def _iso(mdy: str) -> str:
    return datetime.strptime(mdy, "%m/%d/%Y").date().isoformat()


def _amounts(text: str) -> tuple[int, int | None]:
    nums = [int(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", text)]
    if not nums:
        return 0, None
    if "Over" in text:
        return nums[0] + 1, None
    return nums[0], nums[-1] if len(nums) > 1 else nums[0]


def clean_lines(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        # Field labels like "Filing Status: New" render as letters + NULs; drop them.
        if not ln or "\x00" in ln or _NOISE.match(ln):
            continue
        out.append(ln)
    return out


def parse_ptr_text(text: str) -> list[Trade]:
    body = "\n".join(clean_lines(text))
    # Stop at the certification block.
    body = re.split(r"\n\s*(?:Yes\s+No|I CERTIFY)", body)[0]
    anchors = list(_ANCHOR.finditer(body))
    segments = []
    prev = 0
    for m in anchors:
        segments.append(body[prev:m.start()].strip())
        prev = m.end()
    tail = body[prev:].strip()

    # A row split across a page break leaves "Common Stock (CHRW) [ST]" at the start of the next
    # segment. Move that fragment back to the row that is missing its asset-type tag.
    for i in range(len(segments)):
        if not _ATYPE.search(segments[i]):
            nxt = segments[i + 1] if i + 1 < len(segments) else tail
            lines = nxt.split("\n")
            for j, ln in enumerate(lines):
                if _ATYPE.search(ln):
                    frag = "\n".join(lines[: j + 1])
                    segments[i] = f"{segments[i]} {frag}".strip()
                    rest = "\n".join(lines[j + 1:])
                    if i + 1 < len(segments):
                        segments[i + 1] = rest
                    else:
                        tail = rest
                    break

    trades = []
    for seg, m in zip(segments, anchors):
        seg = " ".join(seg.split())
        owner = "self"
        if mo := re.match(r"^(SP|JT|DC)\s+", seg):
            owner, seg = mo.group(1), seg[mo.end():]
        tickers = _TICKER.findall(seg)
        atype = _ATYPE.findall(seg)
        asset = _ATYPE.sub("", seg).strip()
        lo, hi = _amounts(m.group("amount"))
        trades.append(Trade(
            owner=owner, asset=asset, ticker=tickers[-1] if tickers else None,
            asset_type=atype[-1] if atype else None, type=TYPES[m.group("type")],
            date=_iso(m.group("date")), notified=_iso(m.group("notif")),
            amount=" ".join(m.group("amount").split()), amount_min=lo, amount_max=hi,
        ))
    return trades


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# ---------------------------------------------------------------- storage


def _init(ctx: Ctx) -> None:
    db = ctx.db()
    db.executescript("""
    create table if not exists cong_filings (
        doc_id text primary key, chamber text, first text, last text, state_dst text,
        year int, filed text, status text, error text);
    create table if not exists cong_trades (
        doc_id text, chamber text, member text, state_dst text, owner text, asset text,
        ticker text, asset_type text, type text, date text, notified text, filed text,
        amount text, amount_min int, amount_max int, url text);
    create index if not exists cong_t_ticker on cong_trades(ticker);
    create index if not exists cong_t_date on cong_trades(date);
    """)


def available(ctx: Ctx) -> bool:
    row = ctx.db().execute(
        "select 1 from sqlite_master where type='table' and name='cong_trades'").fetchone()
    return row is not None and ctx.db().execute("select count(*) from cong_trades").fetchone()[0] > 0


def house_index(ctx: Ctx, year: int) -> list[dict]:
    """PTR filings listed in the House Clerk's annual index."""
    raw = ctx.http.client.get(HOUSE_INDEX.format(year=year), timeout=60)
    if raw.status_code >= 400:
        raise SourceError(f"House index {year}: HTTP {raw.status_code}")
    with zipfile.ZipFile(io.BytesIO(raw.content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name).decode("utf-8-sig"))
    out = []
    for m in root:
        if m.findtext("FilingType") != "P":
            continue
        out.append({
            "doc_id": m.findtext("DocID"), "first": m.findtext("First") or "", "last": m.findtext("Last") or "",
            "state_dst": m.findtext("StateDst") or "", "year": int(m.findtext("Year") or year),
            "filed": datetime.strptime(m.findtext("FilingDate"), "%m/%d/%Y").date().isoformat(),
        })
    return out


def sync_house(ctx: Ctx, year: int, progress=lambda done, total: None) -> dict[str, int]:
    _init(ctx)
    db = ctx.db()
    filings = house_index(ctx, year)
    have = {r[0] for r in db.execute("select doc_id from cong_filings where status in ('parsed','scanned')")}
    todo = [f for f in filings if f["doc_id"] not in have]
    stats = {"filings": len(filings), "new": len(todo), "trades": 0, "scanned": 0, "errors": 0}
    for i, f in enumerate(todo):
        progress(i, len(todo))
        url = HOUSE_PTR.format(year=f["year"], doc=f["doc_id"])
        member = f"{f['first']} {f['last']}".strip()
        status, error, trades = "parsed", None, []
        try:
            ctx.http._pace("disclosures-clerk.house.gov")
            resp = ctx.http.client.get(url, timeout=60)
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code}")
            text = pdf_text(resp.content)
            trades = parse_ptr_text(text)
            if not trades and len(text.strip()) < 200:
                status = "scanned"  # paper filing: image-only PDF
                stats["scanned"] += 1
        except Exception as e:
            status, error = "error", f"{e.__class__.__name__}: {e}"
            stats["errors"] += 1
        db.execute("delete from cong_trades where doc_id=?", (f["doc_id"],))
        db.executemany(
            "insert into cong_trades values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f["doc_id"], "house", member, f["state_dst"], t.owner, t.asset, t.ticker, t.asset_type,
              t.type, t.date, t.notified, f["filed"], t.amount, t.amount_min, t.amount_max, url)
             for t in trades],
        )
        db.execute("insert or replace into cong_filings values (?,?,?,?,?,?,?,?,?)",
                    (f["doc_id"], "house", f["first"], f["last"], f["state_dst"], f["year"], f["filed"],
                     status, error))
        db.commit()
        stats["trades"] += len(trades)
    progress(len(todo), len(todo))
    return stats


def trades(ctx: Ctx, member: str | None = None, ticker: str | None = None, since: str | None = None,
           limit: int = 200) -> list[dict]:
    _init(ctx)
    q, args = "select * from cong_trades where 1=1", []
    if ticker:
        q += " and upper(ticker)=?"
        args.append(ticker.upper())
    if since:
        q += " and date>=?"
        args.append(since)
    q += " order by date desc"
    rows = [dict(r) for r in ctx.db().execute(q, args)]
    if member:
        rows = [r for r in rows if names.score(member, r["member"]) >= 85
                or names.normalize(member) in names.normalize(r["member"])]
    return rows[:limit]


# ---------------------------------------------------------------- members & committees


@dataclass
class Member:
    bioguide: str
    name: str
    chamber: str
    state: str
    district: int | None
    party: str
    committees: list[dict] = field(default_factory=list)  # {"id","name","title"}


def members(ctx: Ctx) -> dict[str, Member]:
    legs = ctx.http.get(LEGISLATORS, ttl=7 * 86400)
    membership = ctx.http.get(MEMBERSHIP, ttl=7 * 86400)
    comms = ctx.http.get(COMMITTEES, ttl=7 * 86400)
    cname: dict[str, str] = {}
    for c in comms:
        cname[c["thomas_id"]] = c["name"]
        for s in c.get("subcommittees", []):
            cname[c["thomas_id"] + s["thomas_id"]] = f"{c['name']}: {s['name']}"
    out: dict[str, Member] = {}
    for leg in legs:
        t = leg["terms"][-1]
        bid = leg["id"]["bioguide"]
        out[bid] = Member(
            bioguide=bid, name=leg["name"].get("official_full") or f"{leg['name']['first']} {leg['name']['last']}",
            chamber="house" if t["type"] == "rep" else "senate", state=t["state"],
            district=t.get("district"), party=t.get("party", ""),
        )
    for cid, roster in membership.items():
        for m in roster:
            if (mem := out.get(m.get("bioguide"))) is not None:
                mem.committees.append({"id": cid, "name": cname.get(cid, cid), "title": m.get("title")})
    return out


def find_member(ms: dict[str, Member], name: str, state_dst: str | None = None) -> Member | None:
    """Match a disclosure filer to a sitting member (House filers carry state + district)."""
    cands = list(ms.values())
    if state_dst and re.match(r"^[A-Z]{2}\d{1,2}$", state_dst):
        st, dist = state_dst[:2], int(state_dst[2:])
        cands = [m for m in cands if m.chamber == "house" and m.state == st and (m.district or 0) in (dist, 0)]
        if len(cands) == 1:
            return cands[0]
    last = names.normalize(name).split()[-1:] or [""]
    scored = [(names.score(name, m.name), m) for m in cands]
    scored += [(90.0, m) for m in cands if last[0] and last[0] in names.normalize(m.name).split()]
    scored = [x for x in scored if x[0] >= 85]
    return max(scored, key=lambda x: x[0])[1] if scored else None


# Committee keywords -> sectors -> SIC code ranges (EDGAR industry codes).
SECTORS: dict[str, list[tuple[int, int]]] = {
    "defense": [(3480, 3489), (3720, 3729), (3760, 3769), (3795, 3795), (3812, 3812), (3730, 3732)],
    "intel-tech": [(3570, 3579), (3670, 3679), (7370, 7379), (3661, 3669)],
    "health": [(2830, 2836), (3841, 3851), (8000, 8099), (5122, 5122), (6324, 6324)],
    "finance": [(6000, 6799)],
    "energy": [(1300, 1389), (2900, 2999), (4900, 4991), (1220, 1222)],
    "agriculture": [(100, 999), (2000, 2099), (2870, 2879)],
    "transport": [(4000, 4799), (3710, 3716), (3720, 3729), (3740, 3743)],
    "telecom": [(4800, 4899), (3661, 3669)],
    "resources": [(1000, 1099), (1400, 1499), (2400, 2499)],
}
COMMITTEE_SECTORS = [
    (r"Armed Services|Subcommittee on Defense|: Defense$|Tactical Air|Seapower|Strategic Forces", ["defense"]),
    (r"Intelligence|Cybersecurity", ["defense", "intel-tech"]),
    (r"Homeland Security", ["defense", "intel-tech"]),
    (r"Health|Labor, and Pensions|Aging|Veterans", ["health"]),
    (r"Energy and Commerce", ["health", "energy", "telecom", "intel-tech"]),
    (r"Financial Services|Banking|Housing", ["finance"]),
    (r"Ways and Means|Committee on Finance", ["health", "finance"]),
    (r"Agriculture|Nutrition|Forestry", ["agriculture"]),
    (r"Energy and Natural Resources|Committee on Natural Resources|Environment and Public Works", ["energy", "resources"]),
    (r"Transportation and Infrastructure|Aviation|Railroads|Maritime|Commerce, Science", ["transport", "telecom"]),
    (r"Science, Space, and Technology|Communications and Technology", ["intel-tech", "telecom"]),
    (r"Foreign Affairs|Foreign Relations", ["defense"]),
]


# Sectors where federal contracts are big enough to matter for a trade.
CONTRACT_SECTORS = {"defense", "intel-tech", "health", "transport", "telecom", "energy"}


def committee_sectors(committee_name: str) -> list[str]:
    out = []
    for pat, secs in COMMITTEE_SECTORS:
        if re.search(pat, committee_name):
            out += [s for s in secs if s not in out]
    return out


def sic_sectors(sic: int | None) -> list[str]:
    if not sic:
        return []
    return [s for s, ranges in SECTORS.items() if any(a <= sic <= b for a, b in ranges)]


# ---------------------------------------------------------------- flags


@dataclass
class Flagged:
    trade: dict
    member: Member | None
    company: str | None
    sic: str | None
    reasons: list[str]
    score: int
    contracts: list[dict] = field(default_factory=list)


def ticker_map(ctx: Ctx) -> dict[str, dict]:
    data = ctx.http.get(TICKERS, ttl=7 * 86400)
    return {v["ticker"].upper(): {"cik": v["cik_str"], "name": v["title"]} for v in data.values()}


def monthly_contracts(ctx: Ctx, company: str, start: date, end: date) -> dict[tuple[int, int], float]:
    """Federal contract obligations to a company by calendar month."""
    from . import usaspending

    body = {"group": "month", "filters": {
        "recipient_search_text": [company], "award_type_codes": usaspending.CONTRACTS,
        "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}]}}
    data = ctx.http.post(f"{usaspending.API}/search/spending_over_time/", body, ttl=7 * 86400)
    out: dict[tuple[int, int], float] = {}
    for r in data.get("results", []):
        fy, fm = int(r["time_period"]["fiscal_year"]), int(r["time_period"]["month"])
        month = (fm + 8) % 12 + 1  # fiscal month 1 = October
        year = fy - 1 if fm <= 3 else fy
        out[(year, month)] = r.get("Contract_Obligations") or r.get("aggregated_amount") or 0.0
    return out


def _add_months(y: int, m: int, k: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


def contract_spike(monthly: dict[tuple[int, int], float], day: date) -> tuple[float, float]:
    """(obligations in the trade month + next, median of the prior twelve 2-month windows)."""
    from statistics import median

    def two(y: int, m: int) -> float:
        return monthly.get((y, m), 0.0) + monthly.get(_add_months(y, m, 1), 0.0)

    window = two(day.year, day.month)
    prior = [two(*_add_months(day.year, day.month, -k)) for k in range(2, 14)]
    return window, median(prior)


def flag_trades(ctx: Ctx, rows: list[dict], with_contracts: bool = False,
                contract_min: float = 10_000_000, progress=lambda m: None) -> list[Flagged]:
    from . import edgar

    from concurrent.futures import ThreadPoolExecutor

    ms = members(ctx)
    tmap = ticker_map(ctx)
    member_cache: dict[tuple[str, str], Member | None] = {}

    # Prefetch industry codes and contract history in parallel; the loop below is offline.
    ciks = {tmap[t["ticker"].upper()]["cik"] for t in rows if t["ticker"] and t["ticker"].upper() in tmap}

    def sic_of(cik: int) -> tuple[int, tuple[int | None, str | None]]:
        try:
            sub = edgar.submissions(ctx, cik, ttl=30 * 86400)
            return cik, (int(sub["sic"]) if sub.get("sic") else None, sub.get("sicDescription"))
        except (SourceError, ValueError):
            return cik, (None, None)

    progress(f"industry codes for {len(ciks)} companies")
    with ThreadPoolExecutor(max_workers=8) as pool:
        sic_cache = dict(pool.map(sic_of, ciks))

    contract_cache: dict[str, dict] = {}
    if with_contracts and rows:
        d0 = min(date.fromisoformat(r["date"]) for r in rows) - timedelta(days=400)
        wanted = {info["name"] for t in rows if (info := tmap.get((t["ticker"] or "").upper()))
                  and CONTRACT_SECTORS & set(sic_sectors(sic_cache.get(info["cik"], (None, None))[0]))}
        progress(f"contract history for {len(wanted)} companies")

        def hist(company: str) -> tuple[str, dict]:
            try:
                return company, monthly_contracts(ctx, company, d0, date.today())
            except SourceError:
                return company, {}

        with ThreadPoolExecutor(max_workers=6) as pool:
            contract_cache = dict(pool.map(hist, wanted))
    out = []
    for t in rows:
        reasons: list[str] = []
        score = 0
        key = (t["member"], t["state_dst"])
        if key not in member_cache:
            member_cache[key] = find_member(ms, t["member"], t["state_dst"])
        mem = member_cache[key]

        # Late filing (STOCK Act: 45 days).
        traded = date.fromisoformat(t["date"])
        lag = (date.fromisoformat(t["filed"]) - traded).days
        typo = lag < 0 or abs((date.fromisoformat(t["notified"]) - traded).days) > 730
        if typo:
            reasons.append("trade date looks mistyped in the filing")
        elif lag > STOCK_ACT_DAYS:
            reasons.append(f"filed {lag} days after the trade (limit {STOCK_ACT_DAYS})")
            score += 1 if lag <= 60 else 2 if lag <= 180 else 3
        if t["amount_min"] >= 250_001:
            reasons.append(f"large: {t['amount']}")
            score += 1

        company = sic_label = None
        info = tmap.get((t["ticker"] or "").upper())
        if info:
            company = info["name"]
            cik = info["cik"]
            sic, sic_label = sic_cache.get(cik, (None, None))
            secs = set(sic_sectors(sic))
            if mem and secs:
                hits = [c for c in mem.committees if secs & set(committee_sectors(c["name"]))]
                if hits:
                    top = hits[0]
                    title = f" ({top['title']})" if top.get("title") else ""
                    reasons.append(f"sits on {top['name']}{title}; company is {sic_label}")
                    score += 3 if top.get("title") else 2

        flagged = Flagged(t, mem, company, sic_label, reasons, score)
        if company in contract_cache:
            window, norm = contract_spike(contract_cache[company], traded)
            if window >= contract_min and window >= 2 * max(norm, 1.0):
                ratio = f"{window / norm:.1f}x" if norm > 0 else "far above"
                flagged.contracts = [{"window_usd": window, "baseline_usd": norm}]
                flagged.reasons.append(f"${window:,.0f} in federal contracts in the trade month + next, "
                                       f"{ratio} the company's prior-year norm")
                flagged.score += 3
        out.append(flagged)
    out.sort(key=lambda f: f.trade["date"], reverse=True)
    out.sort(key=lambda f: f.score, reverse=True)  # stable: highest score, newest first
    return out

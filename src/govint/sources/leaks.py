"""Product leaks from regulatory filings: FDA device clearances, device listings, drug approvals.

FCC equipment authorizations are the classic source, but the FCC site rejects scripted access,
so for FCC this module only builds links to open in a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .. import names
from ..http import SourceError
from .base import Ctx

FDA = "https://api.fda.gov"
CFDOCS = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs"
FCC_GRANTEE = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm?calledFromFrame=N&grantee_code={code}"
FCC_ID = "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm?mode=Exhibits&calledFromFrame=N&fcc_id={fcc_id}"


@dataclass
class Item:
    source: str
    id: str
    date: str  # ISO
    title: str
    detail: str
    url: str
    company: str
    signal: bool = True  # False for routine paperwork (manufacturing notices, labeling tweaks)


def _iso(d: str) -> str:
    d = d.replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _fda(ctx: Ctx, path: str, search: str, sort: str | None = None, max_rows: int = 1000) -> list[dict]:
    out: list[dict] = []
    while len(out) < max_rows:
        params = {"search": search, "limit": 100, "skip": len(out)}
        if sort:
            params["sort"] = sort
        try:
            data = ctx.http.get(f"{FDA}{path}", params=params, ttl=12 * 3600)
        except SourceError as e:
            if "404" in str(e):  # openFDA answers 404 for "no matches"
                break
            raise
        rows = data.get("results", [])
        out += rows
        total = ((data.get("meta") or {}).get("results") or {}).get("total", 0)
        if not rows or len(out) >= total:
            break
    return out


def _phrase(company: str) -> str:
    # openFDA phrase search on an analyzed field; drop legal suffixes so "Apple Inc." finds "Apple, Inc."
    core = names.normalize(company) or company
    return '"' + core.replace('"', "") + '"'


def _keep(company: str, firm: str) -> bool:
    return names.score(company, firm) >= 80 or names.normalize(company) in names.normalize(firm)


def device_clearances(ctx: Ctx, company: str, since: date) -> list[Item]:
    rng = f"[{since.isoformat()} TO {date.today().isoformat()}]"
    out = []
    for r in _fda(ctx, "/device/510k.json", f"applicant:{_phrase(company)} AND decision_date:{rng}",
                  sort="decision_date:desc"):
        if not _keep(company, r.get("applicant", "")):
            continue
        k = r["k_number"]
        denovo = k.startswith("DEN")
        url = f"{CFDOCS}/cfpmn/denovo.cfm?ID={k}" if denovo else f"{CFDOCS}/cfpmn/pmn.cfm?ID={k}"
        out.append(Item("FDA De Novo" if denovo else "FDA 510(k)", k, r["decision_date"], r["device_name"],
                        f"{r.get('decision_description', '')}; product code {r.get('product_code')}; "
                        f"received {r.get('date_received')}", url, r.get("applicant", "")))
    for r in _fda(ctx, "/device/pma.json", f"applicant:{_phrase(company)} AND decision_date:{rng}",
                  sort="decision_date:desc"):
        if not _keep(company, r.get("applicant", "")):
            continue
        pid = r["pma_number"] + (r.get("supplement_number") or "")
        stype = r.get("supplement_type") or ""
        routine = any(x in stype for x in ("30-Day", "Real-Time", "Special", "135 Review"))
        out.append(Item("FDA PMA", pid, r["decision_date"], r.get("trade_name") or r.get("generic_name", ""),
                        f"{stype or 'original'}: {(r.get('ao_statement') or '')[:160]}",
                        f"{CFDOCS}/cfpma/pma.cfm?id={r['pma_number']}", r.get("applicant", ""),
                        signal=not routine))
    return out


def device_listings(ctx: Ctx, company: str, since: date) -> list[Item]:
    out = []
    for r in _fda(ctx, "/device/registrationlisting.json",
                  f"registration.owner_operator.firm_name:{_phrase(company)}"):
        firm = r["registration"]["owner_operator"]["firm_name"]
        if not _keep(company, firm):
            continue
        for p in r.get("products", []):
            created = p.get("created_date") or ""
            if created and created >= since.isoformat():
                ofda = p.get("openfda") or {}
                dev = ofda.get("device_name", "")
                ks = ofda.get("k_number") or []
                link = (f"{CFDOCS}/cfpmn/pmn.cfm?ID={ks[0]}" if ks else
                        f"{CFDOCS}/cfPCD/classification.cfm?ID={p.get('product_code')}")
                out.append(Item("FDA device listing", f"{p.get('product_code')}:{created}:{(r.get('proprietary_name') or [''])[0]}",
                                created, "; ".join(r.get("proprietary_name") or [dev]) or dev, dev,
                                link, firm))
    return out


def drug_approvals(ctx: Ctx, company: str, since: date) -> list[Item]:
    out = []
    # sponsor_name is an exact, uppercase field: match each word as a prefix.
    words = [w.upper() for w in (names.normalize(company) or company).split()]
    query = " AND ".join(f"sponsor_name:*{w}*" if i else f"sponsor_name:{w}*" for i, w in enumerate(words))
    for r in _fda(ctx, "/drug/drugsfda.json", query):
        if not _keep(company, r.get("sponsor_name", "")):
            continue
        brands = sorted({p.get("brand_name", "") for p in r.get("products", [])})
        for s in r.get("submissions", []):
            d = s.get("submission_status_date") or ""
            if s.get("submission_status") != "AP" or not d or _iso(d) < since.isoformat():
                continue
            original = s.get("submission_type") == "ORIG"
            kind = "new approval" if original else (s.get("submission_class_code_description") or "supplement")
            docs = s.get("application_docs") or []
            url = docs[0]["url"] if docs else (
                f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={r['application_number'][3:]}")
            cls = (s.get("submission_class_code") or "").upper()
            out.append(Item("FDA drug" + (" (NEW)" if original else ""),
                            f"{r['application_number']}-{s.get('submission_type')}{s.get('submission_number')}",
                            _iso(d), ", ".join(brands)[:120], kind, url, r["sponsor_name"],
                            signal=original or cls.startswith("EFFICACY") or cls.startswith("TYPE")))
    return out


def scan(ctx: Ctx, company: str, days: int = 90) -> tuple[list[Item], list[str]]:
    since = date.today() - timedelta(days=days)
    items: list[Item] = []
    notes: list[str] = []
    for label, fn in (("FDA devices", device_clearances), ("FDA listings", device_listings),
                      ("FDA drugs", drug_approvals)):
        try:
            items += fn(ctx, company, since)
        except SourceError as e:
            notes.append(f"{label}: {e}")
    items.sort(key=lambda i: i.date, reverse=True)
    return items, notes


# ---- seen-state, so a cron job can report only what's new


def _init(ctx: Ctx) -> None:
    ctx.db().execute("create table if not exists leaks_seen (id text primary key, first_seen text)")


def only_new(ctx: Ctx, items: list[Item], mark: bool = True) -> list[Item]:
    _init(ctx)
    db = ctx.db()
    keys = [f"{i.source}|{i.id}" for i in items]
    seen = set()
    for k in keys:
        if db.execute("select 1 from leaks_seen where id=?", (k,)).fetchone():
            seen.add(k)
    new = [i for i, k in zip(items, keys) if k not in seen]
    if mark:
        today = date.today().isoformat()
        db.executemany("insert or ignore into leaks_seen values (?,?)", [(k, today) for k in keys])
        db.commit()
    return new


def fcc_links(grantee: str | None = None, fcc_id: str | None = None) -> list[str]:
    out = []
    if grantee:
        out.append(FCC_GRANTEE.format(code=grantee.upper()))
    if fcc_id:
        out.append(FCC_ID.format(fcc_id=fcc_id.upper()))
    return out

"""SBA Paycheck Protection Program loans (data.sba.gov FOIA release), loaded into ppp.sqlite.

`sync` loads the $150k-and-up file (~970k loans) by default; `full=True` adds the 12 files of
smaller loans (~10.5M more, ~5 GB of CSV)."""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import names
from ..graph import AWARD, COMPANY, PERSON, Edge, Node, Result
from .base import Ctx, cite

BASE = "https://data.sba.gov/sites/default/files/distribution/SBA-OCA-2022-07-001/"
RELEASE = "240930"
BIG = f"public_150k_plus_{RELEASE}.csv"
SMALL = [f"public_up_to_150k_{i}_{RELEASE}.csv" for i in range(1, 13)]
SRC = "SBA PPP loan data"
DATASET_URL = "https://data.sba.gov/dataset/ppp-foia"
# Most a borrower could get per employee: 2.5 months of the $100k annual payroll cap.
PER_JOB_CAP = 100_000 / 12 * 2.5
csv.field_size_limit(10_000_000)

COLS = {
    "LoanNumber": "loan", "DateApproved": "approved", "ProcessingMethod": "draw", "BorrowerName": "borrower",
    "BorrowerAddress": "address", "BorrowerCity": "city", "BorrowerState": "state", "BorrowerZip": "zip",
    "LoanStatus": "status", "InitialApprovalAmount": "initial", "CurrentApprovalAmount": "amount",
    "ForgivenessAmount": "forgiven", "ForgivenessDate": "forgiven_date", "JobsReported": "jobs",
    "NAICSCode": "naics", "BusinessType": "business_type", "BusinessAgeDescription": "business_age",
    "OriginatingLender": "lender", "ServicingLenderName": "servicer", "ProjectCountyName": "county",
    "ProjectState": "project_state", "CD": "district", "FranchiseName": "franchise", "NonProfit": "nonprofit",
    "Race": "race", "Gender": "gender", "Veteran": "veteran",
}
NUMERIC = {"initial", "amount", "forgiven", "jobs"}


def path(ctx: Ctx) -> Path:
    return ctx.settings.data_dir / "ppp.sqlite"


def available(ctx: Ctx) -> bool:
    return path(ctx).exists()


_conns: dict[str, sqlite3.Connection] = {}


def db(ctx: Ctx) -> sqlite3.Connection:
    p = str(path(ctx))
    if p not in _conns:
        c = sqlite3.connect(p, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _conns[p] = c
    return _conns[p]


def _iso(mdy: str) -> str:
    try:
        return datetime.strptime(mdy.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def _num(s: str) -> float | None:
    try:
        return float(s) if s.strip() else None
    except ValueError:
        return None


def street_key(address: str, zip_code: str) -> str:
    """Street plus 5-digit ZIP, normalized: the key shells and clustering use."""
    return f"{names.address_key(address)}|{(zip_code or '')[:5]}"


_SCHEMA = f"""
create table if not exists loans ({', '.join(c + (' real' if c in NUMERIC else ' text') for c in COLS.values())},
                                  norm text, akey text, primary key (loan));
create virtual table if not exists names_fts using fts5(norm, content='', contentless_delete=1);
create table if not exists meta (key text primary key, value text);
"""
_INDEXES = """
create index if not exists loans_akey on loans(akey);
create index if not exists loans_zip on loans(substr(zip, 1, 5));
create index if not exists loans_state on loans(state);
create index if not exists loans_lender on loans(lender);
"""


def load_csv(c: sqlite3.Connection, csv_path: Path) -> int:
    dst = list(COLS.values())
    insert = f"insert or replace into loans values ({','.join('?' * (len(dst) + 2))})"
    n, batch = 0, []
    with csv_path.open(encoding="utf-8", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh):
            rec = {d: (r.get(s) or "").strip() for s, d in COLS.items()}
            for k in ("approved", "forgiven_date"):
                rec[k] = _iso(rec[k])
            vals = [(_num(rec[d]) if d in NUMERIC else rec[d]) for d in dst]
            batch.append(vals + [names.normalize(rec["borrower"]), street_key(rec["address"], rec["zip"])])
            if len(batch) >= 20000:
                n += _flush(c, insert, batch)
                batch = []
    return n + _flush(c, insert, batch)


def _flush(c: sqlite3.Connection, insert: str, batch: list) -> int:
    c.executemany(insert, batch)
    # rowid of each loan in `loans` keys the FTS row
    ids = c.execute(f"select rowid, norm from loans where loan in ({','.join('?' * len(batch))})",
                    [b[0] for b in batch]).fetchall()
    c.executemany("insert into names_fts(rowid, norm) values (?,?)", ids)
    return len(batch)


def sync(ctx: Ctx, full: bool = False, progress=lambda _: None) -> int:
    files = [BIG] + (SMALL if full else [])
    dest = path(ctx)
    tmp = dest.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript("pragma journal_mode=off; pragma synchronous=off;" + _SCHEMA)
    total = 0
    try:
        for i, f in enumerate(files, 1):
            local = ctx.settings.data_dir / f
            progress(f"downloading {f} ({i}/{len(files)})")
            ctx.http.download(BASE + f, local)
            try:
                progress(f"loading {f} ({i}/{len(files)}), {total:,} loans so far")
                total += load_csv(c, local)
            finally:
                local.unlink(missing_ok=True)
        progress("indexing")
        c.executescript(_INDEXES)
        c.executemany("insert or replace into meta values (?,?)",
                      [("release", RELEASE), ("scope", "all" if full else "150k_plus"), ("loans", str(total))])
        c.commit()
    finally:
        c.close()
    tmp.replace(dest)
    _conns.pop(str(dest), None)
    return total


def meta(ctx: Ctx) -> dict[str, str]:
    if not available(ctx):
        return {}
    return dict(db(ctx).execute("select key, value from meta").fetchall())


# ---------------------------------------------------------------- queries


@dataclass
class Loan:
    row: dict
    score: float = 100.0
    flags: list[str] = field(default_factory=list)

    def __getitem__(self, k):
        return self.row[k]


def job_cap(r: dict) -> float:
    """Most a loan could be per employee: 2.5 months of capped payroll, 3.5 for second-draw
    loans to hotels and restaurants (NAICS 72)."""
    months = 3.5 if r.get("draw") == "PPS" and str(r.get("naics") or "").startswith("72") else 2.5
    return 100_000 / 12 * months


def flags_for(r: dict) -> list[str]:
    out = []
    amt, jobs = r.get("amount") or 0, r.get("jobs") or 0
    cap = job_cap(r)
    # Reported jobs are self-reported and often rounded, so only flag well past the cap.
    if amt >= 20_000 and jobs and amt / jobs > cap * 1.5:
        out.append(f"${amt / jobs:,.0f} per reported job, {amt / jobs / cap:.1f}x the payroll cap")
    if amt >= 20_000 and not jobs:
        out.append("no jobs reported")
    if (r.get("business_age") or "").startswith("New Business") and amt >= 1_000_000:
        out.append("new business, $1M+ loan")
    if r.get("status") == "Charged Off":
        out.append("charged off (not repaid or forgiven)")
    return out


def _fts(norm: str) -> str:
    return " ".join(f'"{w}"' for w in norm.split() if len(w) > 1)


def search(ctx: Ctx, name: str, threshold: float = 90.0, state: str | None = None, limit: int = 50) -> list[Loan]:
    q = _fts(names.normalize(name))
    if not q:
        return []
    sql = "select l.* from names_fts f join loans l on l.rowid = f.rowid where names_fts match ?"
    args: list = [q]
    if state:
        sql += " and l.state = ?"
        args.append(state.upper())
    out = []
    for r in db(ctx).execute(sql + " limit 5000", args):
        s = names.score(name, r["borrower"])
        if s >= threshold:
            d = dict(r)
            out.append(Loan(d, s, flags_for(d)))
    out.sort(key=lambda x: (-x.score, -(x["amount"] or 0)))
    return out[:limit]


def at_address(ctx: Ctx, address: str, zip_code: str | None = None, limit: int = 500) -> list[Loan]:
    """Loans to borrowers at the same street address. `address` may include city/state/ZIP."""
    m = re.findall(r"\b(\d{5})(?:-\d{4})?\b", address)
    z = zip_code or (m[-1] if m else "")
    if not z:
        return []
    want = names.address_key(address).split()
    rows = db(ctx).execute("select * from loans where substr(zip, 1, 5) = ?", (z[:5],)).fetchall()
    out = []
    for r in rows:
        sk = names.address_key(r["address"]).split()
        # the stored street must appear in the query, house number first
        if sk and set(sk) <= set(want) and want and sk[0] == want[0]:
            out.append(Loan(dict(r), 100.0, flags_for(dict(r))))
    return out[:limit]


def clusters(ctx: Ctx, state: str | None = None, min_borrowers: int = 5, limit: int = 30) -> list[dict]:
    """Street addresses that took loans for many differently named borrowers."""
    where, args = "address != ''", []
    if state:
        where += " and state = ?"
        args.append(state.upper())
    sql = f"""select akey, count(distinct norm) as borrowers, count(*) as loans, sum(amount) as total,
                     min(address) as address, min(city) as city, min(state) as state, min(zip) as zip
              from loans where {where} group by akey having borrowers >= ?
              order by borrowers desc, total desc limit ?"""
    c = db(ctx)
    out = []
    for r in c.execute(sql, (*args, min_borrowers, limit)).fetchall():
        sample = [x[0] for x in c.execute("select distinct borrower from loans where akey=? limit 6", (r["akey"],))]
        out.append(dict(r) | {"sample": sample})
    return out


def top(ctx: Ctx, state: str | None = None, lender: str | None = None, zip_code: str | None = None,
        flagged: bool = False, limit: int = 30) -> list[Loan]:
    sql, args = "select * from loans where 1=1", []
    if state:
        sql += " and state = ?"
        args.append(state.upper())
    if zip_code:
        sql += " and substr(zip, 1, 5) = ?"
        args.append(zip_code[:5])
    if lender:
        sql += " and lender like ?"
        args.append(f"%{lender}%")
    if flagged:
        sql += (f" and amount >= 20000 and (jobs is null or jobs = 0 or amount / jobs > {PER_JOB_CAP * 1.5}"
                " or status = 'Charged Off')")
    rows = db(ctx).execute(sql + " order by amount desc limit ?", (*args, limit)).fetchall()
    return [Loan(dict(r), 100.0, flags_for(dict(r))) for r in rows]


# ---------------------------------------------------------------- graph


def loan_node(loan: Loan) -> Node:
    r = loan.row
    n = Node(id=f"award:ppp:{r['loan']}", kind=AWARD, label=f"PPP loan {r['loan']}",
             props={k: r[k] for k in ("approved", "amount", "forgiven", "jobs", "lender", "status", "draw",
                                      "business_type", "naics", "address", "city", "state")},
             sources=[cite(SRC, DATASET_URL)])
    for f in loan.flags:
        n.flag(f"ppp:{f}")
    return n


def result(ctx: Ctx, subject: Node) -> Result:
    r = Result()
    if not available(ctx) or subject.kind not in (COMPANY, PERSON):
        return r
    for loan in search(ctx, subject.label, threshold=95, limit=10):
        ln = loan_node(loan)
        r.nodes.append(ln)
        r.edges.append(Edge(ln.id, subject.id, "ppp_loan_to", props={"score": round(loan.score, 1),
                                                                     "borrower": loan["borrower"]},
                            source=ln.sources[0]))
    return r

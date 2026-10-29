"""FAA aircraft registry, mirrored locally from the ReleasableAircraft.zip bulk file."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .. import names
from ..graph import AIRCRAFT, Edge, Node, Result
from ..http import SourceError
from .base import Ctx, cite

URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
SRC = "FAA Aircraft Registry"
INQUIRY = "https://registry.faa.gov/AircraftInquiry/Search/NNumberResult?nNumberTxt={n}"

REGISTRANT = {"1": "Individual", "2": "Partnership", "3": "Corporation", "4": "Co-Owned",
              "5": "Government", "7": "LLC", "8": "Non-Citizen Corporation",
              "9": "Non-Citizen Co-Owned"}
AC_TYPE = {"1": "Glider", "2": "Balloon", "3": "Blimp", "4": "Fixed wing single engine",
           "5": "Fixed wing multi engine", "6": "Rotorcraft", "7": "Weight-shift", "8": "Powered parachute",
           "9": "Gyroplane", "H": "Hybrid lift", "O": "Other"}

COLS = ["n_number", "serial", "mfr_mdl_code", "year_mfr", "type_registrant", "name", "street",
        "street2", "city", "state", "zip", "country", "last_action", "cert_issue", "status",
        "mode_s_hex", "fract_owner", "other_names", "expiration"]
SRC_COLS = {"N-NUMBER": "n_number", "SERIAL NUMBER": "serial", "MFR MDL CODE": "mfr_mdl_code",
            "YEAR MFR": "year_mfr", "TYPE REGISTRANT": "type_registrant", "NAME": "name",
            "STREET": "street", "STREET2": "street2", "CITY": "city", "STATE": "state",
            "ZIP CODE": "zip", "COUNTRY": "country", "LAST ACTION DATE": "last_action",
            "CERT ISSUE DATE": "cert_issue", "STATUS CODE": "status", "MODE S CODE HEX": "mode_s_hex",
            "FRACT OWNER": "fract_owner", "EXPIRATION DATE": "expiration"}


def _rows(zf: zipfile.ZipFile, member: str):
    with zf.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace"))
        header = [h.strip().upper() for h in next(reader)]
        for row in reader:
            yield {header[i]: v.strip() for i, v in enumerate(row) if i < len(header)}


INDEXES = [
    "create index if not exists faa_n on faa_master(n_number)",
    "create index if not exists faa_hex on faa_master(mode_s_hex)",
    "create index if not exists faa_norm on faa_master(norm_name)",
]
MIRROR_FILE = "faa.sqlite.gz"
MIRROR_TAG = "faa"


def _load_into(db: sqlite3.Connection, zip_path: Path) -> tuple[int, str]:
    """Load MASTER/ACFTREF from the FAA zip; returns (rows, data date from the zip)."""
    with zipfile.ZipFile(zip_path) as zf:
        members = {Path(m).name.upper(): m for m in zf.namelist()}
        if "MASTER.TXT" not in members:
            raise SourceError(f"{zip_path} has no MASTER.txt; is it the FAA ReleasableAircraft.zip?")
        y, mo, d = zf.getinfo(members["MASTER.TXT"]).date_time[:3]
        data_date = f"{y:04d}-{mo:02d}-{d:02d}"
        db.execute("drop table if exists faa_master")
        db.execute(f"create table faa_master ({', '.join(c + ' text' for c in COLS)}, norm_name text)")
        insert = f"insert into faa_master values ({','.join('?' * (len(COLS) + 1))})"
        batch, n = [], 0
        for r in _rows(zf, members["MASTER.TXT"]):
            rec = {dst: r.get(src, "") for src, dst in SRC_COLS.items()}
            others = [r.get(f"OTHER NAMES({i})", "") for i in range(1, 6)]
            rec["other_names"] = "; ".join(o for o in others if o)
            rec["n_number"] = "N" + rec["n_number"].upper()
            batch.append([rec[c] for c in COLS] + [names.normalize(rec["name"])])
            if len(batch) >= 5000:
                db.executemany(insert, batch)
                n += len(batch)
                batch = []
        db.executemany(insert, batch)
        n += len(batch)
        for ix in INDEXES:
            db.execute(ix)

        db.execute("drop table if exists faa_ref")
        db.execute("create table faa_ref (code text primary key, mfr text, model text, type_acft text, seats text)")
        if "ACFTREF.TXT" in members:
            db.executemany(
                "insert or replace into faa_ref values (?,?,?,?,?)",
                ((r.get("CODE", ""), r.get("MFR", ""), r.get("MODEL", ""), r.get("TYPE-ACFT", ""),
                  r.get("NO-SEATS", "")) for r in _rows(zf, members["ACFTREF.TXT"])),
            )
    _set_meta(db, data_date=data_date, rows=str(n), source="faa.gov")
    db.commit()
    return n, data_date


def _set_meta(db: sqlite3.Connection, **kv: str) -> None:
    db.execute("create table if not exists faa_meta (key text primary key, value text)")
    db.executemany("insert or replace into faa_meta values (?,?)", kv.items())


def meta(ctx: Ctx) -> dict[str, str]:
    try:
        return dict(ctx.db().execute("select key, value from faa_meta").fetchall())
    except sqlite3.OperationalError:
        return {}


def load_zip(ctx: Ctx, zip_path: Path) -> int:
    return _load_into(ctx.db(), zip_path)[0]


# ---- mirror: a prebuilt, gzipped SQLite of the registry, published as a GitHub release asset


def build_mirror(zip_path: Path, out_dir: Path) -> dict:
    """Build faa.sqlite.gz + manifest.json from an FAA zip."""
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "faa.sqlite"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    rows, data_date = _load_into(db, zip_path)
    db.execute("vacuum")
    db.close()
    gz = out_dir / MIRROR_FILE
    with db_path.open("rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    db_path.unlink()
    manifest = {"dataset": "faa", "data_date": data_date, "rows": rows,
                "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "file": MIRROR_FILE, "bytes": gz.stat().st_size, "sha256": _sha256(gz),
                "source": URL}
    (out_dir / "faa.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_mirror(ctx: Ctx, gz_path: Path, manifest: dict) -> int:
    if _sha256(gz_path) != manifest["sha256"]:
        raise SourceError("FAA mirror download is corrupt (checksum mismatch); try again")
    raw = gz_path.with_suffix("")  # faa.sqlite
    with gzip.open(gz_path, "rb") as src, raw.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    db = ctx.db()
    db.execute("attach database ? as m", (str(raw),))
    try:
        for t in ("faa_master", "faa_ref"):
            db.execute(f"drop table if exists main.{t}")
            db.execute(f"create table main.{t} as select * from m.{t}")
        for ix in INDEXES:
            db.execute(ix)
        db.commit()
    finally:
        db.execute("detach database m")
    raw.unlink()
    _set_meta(db, data_date=manifest["data_date"], rows=str(manifest["rows"]), source="mirror")
    db.commit()
    return int(manifest["rows"])


def sync_from_mirror(ctx: Ctx) -> tuple[int, dict]:
    base = ctx.settings.mirror.rstrip("/")
    manifest = ctx.http.get(f"{base}/{MIRROR_TAG}/faa.manifest.json", ttl=0)
    gz = ctx.settings.data_dir / MIRROR_FILE
    ctx.http.download(f"{base}/{MIRROR_TAG}/{MIRROR_FILE}", gz)
    try:
        return install_mirror(ctx, gz, manifest), manifest
    finally:
        gz.unlink(missing_ok=True)


def sync(ctx: Ctx, file: Path | None = None, use_mirror: bool = True) -> tuple[int, str]:
    """Load the registry. Returns (rows, where it came from)."""
    if file is not None:
        n, data_date = _load_into(ctx.db(), file)
        return n, f"{file} (data {data_date})"
    zip_path = ctx.settings.data_dir / "ReleasableAircraft.zip"
    try:
        ctx.http.download(URL, zip_path)
        n, data_date = _load_into(ctx.db(), zip_path)
        return n, f"faa.gov (data {data_date})"
    except SourceError as e:
        if not use_mirror or not ctx.settings.mirror:
            raise SourceError(
                f"FAA download failed ({e}). Download {URL} in a browser, then run "
                "`govint sync faa --file <path>`.") from e
        try:
            n, m = sync_from_mirror(ctx)
        except SourceError as e2:
            raise SourceError(
                f"FAA download failed ({e}) and the mirror failed too ({e2}). Download {URL} in a "
                "browser, then run `govint sync faa --file <path>`.") from e2
        return n, f"mirror {ctx.settings.mirror} (data {m['data_date']}; faa.gov blocked: {e})"


def available(ctx: Ctx) -> bool:
    row = ctx.db().execute(
        "select 1 from sqlite_master where type='table' and name='faa_master'").fetchone()
    return row is not None


_SELECT = """select m.*, r.mfr, r.model, r.type_acft, r.seats
             from faa_master m left join faa_ref r on r.code = m.mfr_mdl_code"""


def _fmt(row) -> dict:
    d = dict(row)
    d["registrant_type"] = REGISTRANT.get(d.get("type_registrant") or "", d.get("type_registrant"))
    d["aircraft_type"] = AC_TYPE.get(d.get("type_acft") or "", d.get("type_acft"))
    return d


def lookup(ctx: Ctx, ident: str) -> list[dict]:
    """By N-number (with or without the N) or Mode S hex code."""
    q = ident.strip().upper()
    n = q if q.startswith("N") else "N" + q
    rows = ctx.db().execute(f"{_SELECT} where m.n_number=? or m.mode_s_hex=?", (n, q)).fetchall()
    return [_fmt(r) for r in rows]


def by_owner(ctx: Ctx, name: str, threshold: float = 90.0, limit: int = 50) -> list[dict]:
    norm = names.normalize(name)
    if not norm:
        return []
    words = sorted(norm.split(), key=len, reverse=True)
    rows = ctx.db().execute(f"{_SELECT} where m.norm_name like ? limit 2000", (f"%{words[0]}%",)).fetchall()
    out = []
    for r in rows:
        s = names.score(norm, r["norm_name"])
        if s >= threshold:
            d = _fmt(r)
            d["score"] = round(s, 1)
            out.append(d)
    return sorted(out, key=lambda d: -d["score"])[:limit]


def aircraft_node(a: dict) -> Node:
    model = " ".join(x for x in (a.get("year_mfr"), (a.get("mfr") or "").strip(), (a.get("model") or "").strip()) if x)
    return Node(
        id=f"aircraft:{a['n_number']}",
        kind=AIRCRAFT,
        label=f"{a['n_number']} {model}".strip(),
        props={k: a.get(k) for k in ("n_number", "serial", "mode_s_hex", "aircraft_type", "seats",
                                       "registrant_type", "name", "city", "state", "cert_issue",
                                       "expiration", "status")},
        sources=[cite(SRC, INQUIRY.format(n=a["n_number"][1:]))],
    )


def owner_result(ctx: Ctx, subject: Node) -> Result:
    r = Result()
    if not available(ctx):
        return r
    for a in by_owner(ctx, subject.label):
        node = aircraft_node(a)
        r.nodes.append(node)
        r.edges.append(Edge(subject.id, node.id, "registered_owner_of",
                            props={"score": a["score"]}, source=node.sources[0]))
    return r

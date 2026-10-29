"""Consolidated Screening List (trade.gov): 11 US sanctions/export lists, screened offline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

from .. import names
from ..graph import SANCTION, Edge, Node, Result
from .base import Ctx, cite

URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json"
SRC = "Consolidated Screening List (trade.gov)"
DEFAULT_THRESHOLD = 88.0

KEEP = ("id", "source", "name", "alt_names", "programs", "country", "type", "start_date",
        "end_date", "remarks", "source_list_url", "source_information_url", "addresses",
        "ids", "dates_of_birth", "nationalities", "federal_register_notice")


def path(ctx: Ctx) -> Path:
    return ctx.settings.data_dir / "csl.json"


def sync(ctx: Ctx) -> int:
    raw = ctx.settings.data_dir / "csl.raw.json"
    ctx.http.download(URL, raw)
    records = json.loads(raw.read_text())["results"]
    slim = [{k: r.get(k) for k in KEEP} for r in records]
    path(ctx).write_text(json.dumps(slim))
    raw.unlink()
    _load.cache_clear()
    return len(slim)


@lru_cache(maxsize=1)
def _load(p: str, mtime: float) -> tuple[list[dict], list[str], list[int]]:
    records = json.loads(Path(p).read_text())
    keys, owners = [], []
    for i, r in enumerate(records):
        for n in [r["name"], *(r.get("alt_names") or [])]:
            if n and (norm := names.normalize(n)):
                keys.append(norm)
                owners.append(i)
    return records, keys, owners


def available(ctx: Ctx) -> bool:
    return path(ctx).exists()


@dataclass
class Hit:
    score: float
    matched: str
    record: dict


def record_countries(rec: dict) -> set[str]:
    out = {a.get("country") for a in rec.get("addresses") or [] if a.get("country")}
    if rec.get("country"):
        out.add(rec["country"])
    return {c.upper() for c in out}


def screen(ctx: Ctx, name: str, threshold: float = DEFAULT_THRESHOLD, limit: int = 10,
           country: str | None = None) -> list[Hit]:
    """Fuzzy-screen a name. With `country`, drop listed entities known to be elsewhere."""
    p = path(ctx)
    if not p.exists():
        return []
    records, keys, owners = _load(str(p), p.stat().st_mtime)
    query = names.normalize(name)
    if not query:
        return []
    # Cheap prefilter on token_set, then rescore with the stricter blended score.
    cands = process.extract(query, keys, scorer=fuzz.token_set_ratio, score_cutoff=threshold - 15,
                            limit=200)
    best: dict[int, Hit] = {}
    for key, _, idx in cands:
        rec_i = owners[idx]
        s = names.score(name, key, person=records[rec_i].get("type") == "Individual")
        if country and (where := record_countries(records[rec_i])) and country.upper() not in where:
            continue
        if s >= threshold and (rec_i not in best or s > best[rec_i].score):
            best[rec_i] = Hit(s, key, records[rec_i])
    return sorted(best.values(), key=lambda h: -h.score)[:limit]


def screen_result(ctx: Ctx, subject: Node, threshold: float = DEFAULT_THRESHOLD) -> Result:
    r = Result()
    for h in screen(ctx, subject.label, threshold):
        rec = h.record
        url = rec.get("source_information_url") or rec.get("source_list_url") or URL
        src = cite(f"{SRC}: {rec['source']}", url)
        node = Node(
            id=f"sanction:{rec['id']}",
            kind=SANCTION,
            label=rec["name"],
            props={
                "list": rec["source"], "programs": rec.get("programs"), "country": rec.get("country"),
                "type": rec.get("type"), "since": rec.get("start_date"), "remarks": rec.get("remarks"),
            },
            sources=[src],
            flags=["listed"],
        )
        r.nodes.append(node)
        r.edges.append(Edge(subject.id, node.id, "possible_match",
                            props={"score": round(h.score, 1)}, source=src))
        if h.score >= 95:
            subject.flag("sanctions-match")
        else:
            subject.flag("sanctions-possible")
    return r

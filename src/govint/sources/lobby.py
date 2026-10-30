"""Federal lobbying disclosures (Lobbying Disclosure Act, lda.gov): who a company pays to lobby,
on what, which agencies they hit, and which of their lobbyists used to work in government."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .. import names
from ..graph import COMPANY, PERSON, Edge, Node, Result
from .base import Ctx, cite

API = "https://lda.gov/api/v1"
SRC = "Lobbying Disclosure Act (lda.gov)"
PAGE_SIZE = 25  # the API caps anonymous pages at 25


def _get_all(ctx: Ctx, params: dict, max_pages: int) -> list[dict]:
    out: list[dict] = []
    params = {**params, "page_size": PAGE_SIZE}
    url = f"{API}/filings/"
    key = ctx.settings.lda_key
    headers = {"Authorization": f"Token {key}"} if key else None
    for page in range(1, max_pages + 1):
        data = ctx.http.get(url, params={**params, "page": page}, ttl=7 * 86400, headers=headers)
        out += data.get("results", [])
        if not data.get("next"):
            break
    return out


def filings(ctx: Ctx, client: str | None = None, firm: str | None = None, years: int = 2,
            max_pages: int = 12) -> list[dict]:
    """Quarterly reports for a client or lobbying firm, newest years first, amendments collapsed."""
    this_year = date.today().year
    raw: list[dict] = []
    for y in range(this_year, this_year - years, -1):
        params = {"filing_year": y}
        if client:
            params["client_name"] = client
        if firm:
            params["registrant_name"] = firm
        raw += _get_all(ctx, params, max_pages)
    want = client or firm or ""
    latest: dict[tuple, dict] = {}
    for f in raw:
        who = f["client"]["name"] if client else f["registrant"]["name"]
        if names.score(want, who) < 88:
            continue
        key = (f["registrant"]["id"], f["client"]["id"], f["filing_year"], f["filing_period"])
        if key not in latest or f["dt_posted"] > latest[key]["dt_posted"]:
            latest[key] = f
    return sorted(latest.values(), key=lambda f: (f["filing_year"], f["dt_posted"]), reverse=True)


def amount(f: dict) -> float:
    v = f.get("income") if f.get("income") is not None else f.get("expenses")
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


@dataclass
class Summary:
    name: str
    filings: int = 0
    by_year: dict[int, float] = field(default_factory=dict)
    parties: list[dict] = field(default_factory=list)  # firms hired (client view) or clients (firm view)
    issues: list[tuple[str, int]] = field(default_factory=list)
    agencies: list[tuple[str, int]] = field(default_factory=list)
    revolving: list[dict] = field(default_factory=list)  # lobbyists with prior government jobs
    foreign: list[dict] = field(default_factory=list)  # foreign entities with an interest
    descriptions: list[str] = field(default_factory=list)


def summarize(name: str, fs: list[dict], firm_view: bool = False) -> Summary:
    s = Summary(name, filings=len(fs))
    by_year: dict[int, float] = defaultdict(float)
    parties: dict[str, dict] = {}
    issues, agencies = Counter(), Counter()
    revolving: dict[str, dict] = {}
    foreign: dict[str, dict] = {}
    descs: list[str] = []
    for f in fs:
        amt = amount(f)
        by_year[f["filing_year"]] += amt
        other = f["client"] if firm_view else f["registrant"]
        own = names.score(f["client"]["name"], f["registrant"]["name"]) >= 90  # in-house lobbying
        p = parties.setdefault(other["name"], {"name": other["name"], "amount": 0.0, "last": "", "self": own})
        p["amount"] += amt
        p["last"] = max(p["last"], f"{f['filing_year']} {f['filing_period'].replace('_', ' ')}")
        p["url"] = f["filing_document_url"]
        for a in f.get("lobbying_activities") or []:
            issues[a.get("general_issue_code_display") or a.get("general_issue_code") or "?"] += 1
            if (d := (a.get("description") or "").strip()) and d not in descs:
                descs.append(d)
            for g in a.get("government_entities") or []:
                agencies[g.get("name") or "?"] += 1
            for lb in a.get("lobbyists") or []:
                cp = (lb.get("covered_position") or "").strip()
                if not cp or cp.lower() in ("n/a", "na", "none"):
                    continue
                person = lb["lobbyist"]
                full = " ".join(x for x in (person.get("first_name"), person.get("last_name")) if x).title()
                revolving.setdefault(full, {"name": full, "position": cp, "firm": f["registrant"]["name"],
                                            "url": f["filing_document_url"]})
        for fe in f.get("foreign_entities") or []:
            foreign.setdefault(fe.get("name") or "?", {"name": fe.get("name"), "country": fe.get("country_display")
                                                       or fe.get("country"), "url": f["filing_document_url"]})
    s.by_year = dict(sorted(by_year.items(), reverse=True))
    s.parties = sorted(parties.values(), key=lambda p: -p["amount"])
    s.issues = issues.most_common(12)
    s.agencies = agencies.most_common(12)
    s.revolving = sorted(revolving.values(), key=lambda r: ("congress" not in r["position"].lower(), r["name"]))
    s.foreign = list(foreign.values())
    s.descriptions = descs[:15]
    return s


def result(ctx: Ctx, subject: Node) -> Result:
    """Graph: subject -> lobbying firms it pays, and the ex-officials lobbying for it."""
    r = Result()
    if subject.kind != COMPANY:
        return r
    fs = filings(ctx, client=subject.label, years=2, max_pages=3)
    if not fs:
        return r
    s = summarize(subject.label, fs)
    subject.props["lobbying_last_2y"] = round(sum(s.by_year.values()))
    for p in s.parties[:12]:
        src = cite(SRC, p["url"])
        if p["self"]:
            continue
        n = Node(id=f"company:lda:{names.normalize(p['name'])}", kind=COMPANY, label=p["name"],
                 props={"role": "lobbying firm", "screen": False}, sources=[src])
        r.nodes.append(n)
        r.edges.append(Edge(subject.id, n.id, "pays_lobbyist", props={"amount": p["amount"], "last": p["last"]},
                            source=src))
    for lb in s.revolving[:15]:
        src = cite(SRC, lb["url"])
        n = Node(id=f"person:lda:{names.normalize(lb['name'])}", kind=PERSON, label=lb["name"],
                 props={"former_position": lb["position"], "firm": lb["firm"], "screen": False}, sources=[src])
        r.nodes.append(n)
        r.edges.append(Edge(n.id, subject.id, "lobbies_for", props={"former_position": lb["position"]},
                            source=src))
    if s.revolving:
        subject.flag(f"revolving-door:{len(s.revolving)}")
    for fe in s.foreign:
        subject.flag(f"lobbying-foreign-interest:{fe['name']} ({fe['country']})")
    return r

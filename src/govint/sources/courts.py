"""Federal court records via CourtListener (Free Law Project): RECAP dockets from PACER for district,
bankruptcy and appeals courts, plus written opinions. Needs a free API token."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl
from collections import Counter
from dataclasses import dataclass, field

from .. import names
from ..graph import CASE, COMPANY, PERSON, Edge, Node, Result
from ..http import SourceError
from .base import Ctx, cite

API = "https://www.courtlistener.com/api/rest/v4"
SITE = "https://www.courtlistener.com"
SRC = "CourtListener (RECAP/PACER)"
TOKEN_HELP = ("CourtListener needs a free API token: sign up at https://www.courtlistener.com/sign-in/, "
              "copy it from https://www.courtlistener.com/profile/api/, then `govint config courtlistener_token <token>`.")

# Nature-of-suit text worth calling out.
NOTABLE = {
    "false claims": "False Claims Act (fraud against the government)",
    "securities": "securities",
    "racketeer": "RICO",
    "antitrust": "antitrust",
    "forfeiture": "asset forfeiture",
    "fraud": "fraud",
    "trade secret": "trade secrets",
    "environmental": "environmental",
    "qui tam": "qui tam",
}
_CRIMINAL = re.compile(r"\d+:\d{2}-cr-\d+|-cr-|\bcr\b", re.I)


@dataclass
class Case:
    name: str
    court: str
    court_id: str
    docket: str
    filed: str
    terminated: str
    nature: str
    cause: str
    url: str
    kind: str  # docket or opinion
    parties: list[str] = field(default_factory=list)
    judge: str = ""
    snippet: str = ""

    @property
    def criminal(self) -> bool:
        return bool(_CRIMINAL.search(self.docket or "")) or "criminal" in (self.nature or "").lower()

    @property
    def bankruptcy(self) -> bool:
        return self.court_id.endswith("b") and self.court_id not in ("ca", "")

    @property
    def tags(self) -> list[str]:
        out = []
        if self.criminal:
            out.append("criminal")
        if self.bankruptcy:
            out.append("bankruptcy")
        hay = f"{self.nature} {self.cause}".lower()
        out += [label for k, label in NOTABLE.items() if k in hay and label not in out]
        if "united states" in self.name.lower() and "criminal" not in out:
            out.append("US government party")
        return out


def _headers(ctx: Ctx) -> dict:
    tok = ctx.settings.courtlistener_token
    if not tok:
        raise SourceError(TOKEN_HELP)
    return {"Authorization": f"Token {tok}"}


def _get(ctx: Ctx, path: str, params: dict, ttl: float = 86400) -> dict:
    try:
        return ctx.http.get(f"{API}/{path}", params=params, ttl=ttl, headers=_headers(ctx))
    except SourceError as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            raise SourceError(f"CourtListener rejected the token. {TOKEN_HELP}") from e
        raise


def _case(r: dict, kind: str) -> Case:
    url = r.get("docket_absolute_url") or r.get("absolute_url") or ""
    return Case(
        name=r.get("caseName") or r.get("case_name") or "?",
        court=r.get("court") or r.get("court_citation_string") or "",
        court_id=r.get("court_id") or "",
        docket=r.get("docketNumber") or r.get("docket_number") or "",
        filed=(r.get("dateFiled") or "")[:10],
        terminated=(r.get("dateTerminated") or "")[:10],
        nature=r.get("suitNature") or r.get("nature_of_suit") or "",
        cause=r.get("cause") or "",
        url=SITE + url if url.startswith("/") else url,
        kind=kind,
        parties=list(r.get("party") or [])[:20],
        judge=r.get("assignedTo") or r.get("judge") or "",
        snippet=re.sub(r"<[^>]+>", "", (r.get("snippet") or "").strip())[:240],
    )


def search(ctx: Ctx, name: str, opinions: bool = False, limit: int = 40, since: str | None = None) -> tuple[int, list[Case]]:
    """Dockets where `name` is a party (or opinions mentioning it in the case name)."""
    quoted = '"' + name.replace('"', "") + '"'
    q = f"caseName:({quoted})" if opinions else f"party:({quoted})"
    if since:
        q += f" AND dateFiled:[{since} TO *]"
    params: dict = {"type": "o" if opinions else "r", "q": q, "order_by": "dateFiled desc"}
    out: list[Case] = []
    total = 0
    url_params = params
    path = "search/"
    while len(out) < limit:
        data = _get(ctx, path, url_params)
        total = data.get("count") or total
        out += [_case(r, "opinion" if opinions else "docket") for r in data.get("results", [])]
        nxt = data.get("next")
        if not nxt or not data.get("results"):
            break
        path, _, query = nxt.partition("/api/rest/v4/")[2].partition("?")
        url_params = dict(parse_qsl(query))
    # Keep cases where a party (or the case name) actually matches; party_name is a loose search.
    keep = []
    for c in out:
        cands = c.parties + re.split(r"\s+v\.?\s+|\bex rel\.?\s+", c.name)
        if any(names.score(name, p) >= 88 for p in cands if p.strip()):
            keep.append(c)
    return total, keep[:limit]


@dataclass
class Summary:
    total: int
    cases: list[Case]
    by_court: list[tuple[str, int]]
    by_nature: list[tuple[str, int]]
    tagged: dict[str, int]


def summarize(total: int, cases: list[Case]) -> Summary:
    tags = Counter(t for c in cases for t in c.tags)
    return Summary(total, cases, Counter(c.court for c in cases).most_common(8),
                   Counter(c.nature for c in cases if c.nature).most_common(8), dict(tags))


def result(ctx: Ctx, subject: Node) -> Result:
    r = Result()
    if not ctx.settings.courtlistener_token or subject.kind not in (COMPANY, PERSON):
        return r
    _total, cases = search(ctx, subject.label, limit=20)
    for c in cases:
        src = cite(SRC, c.url)
        n = Node(id=f"case:cl:{c.court_id}:{c.docket}", kind=CASE, label=c.name,
                 props={"court": c.court, "docket": c.docket, "filed": c.filed, "terminated": c.terminated,
                        "nature": c.nature, "tags": c.tags}, sources=[src])
        r.nodes.append(n)
        r.edges.append(Edge(subject.id, n.id, "party_to", props={"tags": c.tags}, source=src))
        for t in c.tags:
            if t in ("criminal", "False Claims Act (fraud against the government)", "bankruptcy", "qui tam", "RICO"):
                subject.flag(f"court:{t}")
    return r

"""Pivot engine: fan a name out across every source and link what comes back."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import names
from .graph import COMPANY, PERSON, Edge, Graph, Node, Result
from .http import SourceError
from .sources import congress, courts, csl, edgar, faa, fara, fec, gleif, lobby, offshore, ppp, sam, usaspending
from .sources.base import Ctx, cite

Progress = Callable[[str], None]

# Only these kinds get screened against local datasets (CSL, FAA).
SCREENABLE = (COMPANY, PERSON)


def _safe(label: str, fn: Callable[[], Result]) -> Result:
    try:
        return fn()
    except SourceError as e:
        return Result(notes=[f"{label}: {e}"])
    except Exception as e:  # keep the pivot alive if one source changes shape
        return Result(notes=[f"{label}: unexpected {e.__class__.__name__}: {e}"])


def _edgar(ctx: Ctx, subject: Node, insiders: bool = True) -> Result:
    hits = edgar.search(ctx, subject.label)
    if not hits or hits[0]["score"] < 90:
        return Result()
    best = hits[0]
    r = edgar.profile(ctx, best["cik"])
    company = r.nodes[0]
    r.edges.append(Edge(subject.id, company.id, "same_as", props={"score": round(best["score"], 1)},
                        source=company.sources[0]))
    if insiders:
        r.extend(edgar.insiders_result(ctx, best["cik"], company.label))
    r.extend(_congress(ctx, company))
    # EDGAR often knows the LEI; hand it to GLEIF so we don't rely on fuzzy names.
    if lei := company.props.get("lei"):
        g = gleif.result(ctx, company, lei=lei)
        r.extend(g)
    return r


def _congress(ctx: Ctx, company: Node) -> Result:
    """Members of Congress who disclosed trades in this company's stock."""
    r = Result()
    if not congress.available(ctx):
        return r
    for ticker in company.props.get("tickers") or []:
        for t in congress.trades(ctx, ticker=ticker, limit=100):
            who = Node(id=f"person:congress:{names.normalize(t['member'])}", kind=PERSON,
                       label=f"Rep. {t['member']} ({t['state_dst']})", props={"chamber": t["chamber"]},
                       sources=[cite("House Clerk PTR", t["url"])])
            r.nodes.append(who)
            r.edges.append(Edge(who.id, company.id, f"congress_trade:{t['doc_id']}:{t['date']}:{t['type']}:{t['amount_min']}",
                                props={k: t[k] for k in ("type", "date", "filed", "amount", "owner", "ticker")},
                                source=cite("House Clerk PTR", t["url"])))
    return r


def _remote(ctx: Ctx, subject: Node, full: bool) -> list[tuple[str, Callable[[], Result]]]:
    jobs = [
        ("EDGAR", lambda: _edgar(ctx, subject, insiders=full)),
        ("GLEIF", lambda: gleif.result(ctx, subject)),
    ]
    if full:
        jobs += [
            ("USAspending", lambda: usaspending.result(ctx, subject)),
            ("FEC", lambda: fec.result(ctx, subject)),
            ("SAM.gov", lambda: sam.result(ctx, subject)),
            ("Lobbying", lambda: lobby.result(ctx, subject)),
            ("CourtListener", lambda: courts.result(ctx, subject)),
        ]
    return jobs


def _screen_local(ctx: Ctx, g: Graph, nodes: list[Node], progress: Progress) -> None:
    targets = [n for n in nodes if n.kind in SCREENABLE and n.props.get("screen", True)]
    if not targets:
        return
    have_csl, have_faa, have_fara = csl.available(ctx), faa.available(ctx), fara.available(ctx)
    have_icij, have_ppp = offshore.available(ctx), ppp.available(ctx)
    progress(f"screening {len(targets)} names locally")
    for n in targets:
        if have_csl:
            g.merge(_safe("CSL", lambda n=n: csl.screen_result(ctx, n)))
        if have_faa:
            g.merge(_safe("FAA", lambda n=n: faa.owner_result(ctx, n)))
        if have_fara:
            g.merge(_safe("FARA", lambda n=n: fara.result(ctx, n)))
        if have_icij:
            g.merge(_safe("ICIJ", lambda n=n: offshore.result(ctx, n)))
        if have_ppp and n.kind == COMPANY:
            g.merge(_safe("PPP", lambda n=n: ppp.result(ctx, n)))


def pivot(ctx: Ctx, query: str, depth: int = 1, related_budget: int = 6,
          progress: Progress = lambda _: None) -> tuple[Graph, Node]:
    g = Graph()
    seed = g.add_node(Node(id=f"query:{names.normalize(query)}", kind=COMPANY, label=query,
                           props={"query": True}, sources=[cite("query", "")]))
    if not csl.available(ctx):
        g.notes.append("CSL not synced: run `govint sync csl` to enable sanctions screening.")
    if not faa.available(ctx):
        g.notes.append("FAA registry not synced: run `govint sync faa` to enable aircraft lookups.")
    if not ctx.settings.courtlistener_token:
        g.notes.append("Court records skipped: set `govint config courtlistener_token <token>` (free account).")

    def run(subjects: list[Node], full: bool) -> list[Node]:
        jobs = [(f"{label} ({s.label})", fn) for s in subjects for label, fn in _remote(ctx, s, full)]
        new: list[Node] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(_safe, label, fn): label for label, fn in jobs}
            for fut, label in futs.items():
                progress(label)
                new += g.merge(fut.result())
        return new

    progress(f"querying sources for {query!r}")
    found = run([seed], full=True)
    _screen_local(ctx, g, [seed, *found], progress)

    if depth >= 2:
        related = [n for n in found if n.kind == COMPANY and not n.props.get("query")]
        related = [n for n in related if n.props.get("lei") or n.props.get("uei") or n.props.get("cik")]
        related = related[:related_budget]
        if related:
            progress(f"depth 2: {len(related)} related companies")
            found2 = run(related, full=False)
            _screen_local(ctx, g, found2, progress)
    return g, seed

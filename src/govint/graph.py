"""Entity graph: nodes from every source, merged by id, with sourced edges."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import networkx as nx

# Node kinds
COMPANY = "company"
PERSON = "person"
COMMITTEE = "committee"
CANDIDATE = "candidate"
SANCTION = "sanction"
AWARD = "award"
AGENCY = "agency"
AIRCRAFT = "aircraft"
ADDRESS = "address"
CASE = "case"


@dataclass
class Node:
    id: str
    kind: str
    label: str
    props: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, str]] = field(default_factory=list)  # [{"name":..,"url":..}]
    flags: list[str] = field(default_factory=list)  # risk flags, e.g. "sanctions-match"

    def flag(self, f: str) -> None:
        if f not in self.flags:
            self.flags.append(f)


@dataclass
class Edge:
    src: str
    dst: str
    rel: str
    props: dict[str, Any] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    """What a source contributes: nodes, edges, and non-fatal notes."""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def extend(self, other: "Result") -> None:
        self.nodes += other.nodes
        self.edges += other.edges
        self.notes += other.notes


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self.notes: list[str] = []

    def add_node(self, n: Node) -> Node:
        cur = self.nodes.get(n.id)
        if cur is None:
            self.nodes[n.id] = n
            return n
        for k, v in n.props.items():
            if v not in (None, "", [], {}):
                cur.props.setdefault(k, v)
        for s in n.sources:
            if s not in cur.sources:
                cur.sources.append(s)
        for f in n.flags:
            if f not in cur.flags:
                cur.flags.append(f)
        return cur

    def add_edge(self, e: Edge) -> None:
        if e.src == e.dst:
            return
        self.edges.setdefault((e.src, e.dst, e.rel), e)

    def merge(self, r: Result) -> list[Node]:
        """Add a source result; returns the nodes that were new to the graph."""
        new = []
        for n in r.nodes:
            if n.id not in self.nodes:
                new.append(n)
            self.add_node(n)
        for e in r.edges:
            if e.src in self.nodes and e.dst in self.nodes:
                self.add_edge(e)
        self.notes += [x for x in r.notes if x not in self.notes]
        return new

    def neighbors(self, node_id: str) -> Iterable[tuple[Edge, Node]]:
        for (s, d, _), e in self.edges.items():
            if s == node_id:
                yield e, self.nodes[d]
            elif d == node_id:
                yield e, self.nodes[s]

    def of_kind(self, kind: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]

    # ---- export ----

    def to_dict(self) -> dict:
        return {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges.values()],
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_networkx(self) -> nx.MultiDiGraph:
        g = nx.MultiDiGraph()
        for n in self.nodes.values():
            g.add_node(n.id, kind=n.kind, label=n.label, flags=",".join(n.flags))
        for e in self.edges.values():
            g.add_edge(e.src, e.dst, rel=e.rel)
        return g

    def to_dot(self) -> str:
        colors = {
            COMPANY: "#4C78A8", PERSON: "#F58518", COMMITTEE: "#54A24B", CANDIDATE: "#B279A2",
            SANCTION: "#E45756", AWARD: "#72B7B2", AGENCY: "#9D755D", AIRCRAFT: "#EECA3B",
            ADDRESS: "#BAB0AC", CASE: "#B279A2",
        }

        def q(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        lines = ["digraph govint {", "  rankdir=LR; node [style=filled fontname=Helvetica fontcolor=white];"]
        for n in self.nodes.values():
            color = "#E45756" if n.flags else colors.get(n.kind, "#888888")
            label = f"{n.label}\\n[{n.kind}]"
            lines.append(f"  {q(n.id)} [label={q(label)} fillcolor={q(color)}];")
        for e in self.edges.values():
            lines.append(f"  {q(e.src)} -> {q(e.dst)} [label={q(e.rel)} fontsize=9];")
        lines.append("}")
        return "\n".join(lines)

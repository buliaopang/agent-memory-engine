"""Deterministic topological ordering of the structural DAG (ADR-019). Pure, no I/O."""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable

from ame.errors import CycleDetectedError


def topological_order(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[str]:
    """Kahn's algorithm; ties broken by memory_id so the output is stable.

    Edge (a, b) means a is placed before b. Isolated nodes are included.
    Raises CycleDetectedError if the edges contain a cycle (should be impossible: DB guard).
    """
    all_nodes = set(nodes)
    adj: dict[str, set[str]] = defaultdict(set)
    indeg: dict[str, int] = dict.fromkeys(all_nodes, 0)
    for a, b in edges:
        if a == b:
            raise CycleDetectedError("self-loop in structural edges", {"memory_id": a})
        indeg.setdefault(a, 0)
        indeg.setdefault(b, 0)
        if b not in adj[a]:
            adj[a].add(b)
            indeg[b] += 1
    heap = [n for n, d in indeg.items() if d == 0]
    heapq.heapify(heap)
    out: list[str] = []
    while heap:
        n = heapq.heappop(heap)
        out.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                heapq.heappush(heap, m)
    if len(out) != len(indeg):
        left = sorted(n for n, d in indeg.items() if d > 0)
        raise CycleDetectedError("cycle in active structural edges", {"nodes": left[:50]})
    return out

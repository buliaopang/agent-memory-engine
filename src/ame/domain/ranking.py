"""Deterministic, explainable search ranking (ADR-020, P2-5 / Q17). Pure functions, no I/O.

score = w_vector * vector + w_graph * graph + w_importance * importance
        + w_confidence * confidence + w_freshness * freshness          (vector available)
score = w_vector * keyword + ... (same other terms)                    (no vector: lexical_graph)

The keyword score takes the vector weight when no vector similarity exists for a result, so
the weights always sum to 1. A missing vector similarity is reported as None ("not used"),
never as a made-up number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

MAX_TERMS = 16


@dataclass(frozen=True)
class Weights:
    vector: float = 0.35
    graph: float = 0.20
    importance: float = 0.15
    confidence: float = 0.20
    freshness: float = 0.10

    def __post_init__(self) -> None:
        vals = (self.vector, self.graph, self.importance, self.confidence, self.freshness)
        if any(v < 0 or v > 1 for v in vals):
            raise ValueError("search weights must be within 0..1")
        if not math.isclose(sum(vals), 1.0, abs_tol=1e-9):
            raise ValueError("search weights must sum to 1.0")


@dataclass(frozen=True)
class Components:
    keyword: float
    vector: float | None
    graph: float
    importance: float
    confidence: float
    freshness: float


def query_terms(query: str) -> list[str]:
    """Whitespace-separated, de-duplicated (case-insensitive), at most MAX_TERMS terms."""
    seen: set[str] = set()
    out: list[str] = []
    for t in query.split():
        k = t.casefold()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out[:MAX_TERMS]


def keyword_score(matched_terms: int, total_terms: int) -> float:
    return 0.0 if total_terms <= 0 else min(1.0, matched_terms / total_terms)


def graph_score(linked_hits: int, saturation: int = 3) -> float:
    """Share of saturation reached by links to other hits of the same query (0..1)."""
    return min(1.0, max(0, linked_hits) / saturation)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def freshness_score(updated_at: str, now: datetime, half_life_days: float = 30.0) -> float:
    """1.0 for just-updated, halves every half_life_days. Future timestamps count as now."""
    age = (now - _parse(updated_at)).total_seconds() / 86400.0
    return math.pow(0.5, max(0.0, age) / half_life_days)


def total_score(c: Components, w: Weights) -> float:
    first = c.vector if c.vector is not None else c.keyword
    s = (
        w.vector * first
        + w.graph * c.graph
        + w.importance * c.importance
        + w.confidence * c.confidence
        + w.freshness * c.freshness
    )
    return round(s, 6)


def weights_used(c: Components, w: Weights) -> dict[str, float]:
    return {
        "vector": w.vector if c.vector is not None else 0.0,
        "keyword": 0.0 if c.vector is not None else w.vector,
        "graph": w.graph,
        "importance": w.importance,
        "confidence": w.confidence,
        "freshness": w.freshness,
    }

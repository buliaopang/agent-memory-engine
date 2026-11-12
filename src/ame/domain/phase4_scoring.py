"""Phase 4 deterministic scoring (ADR-029 decay/feedback, ADR-031 spine, ADR-032 distill).
Pure functions, no I/O, no LLM. Every score is explainable from its components.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

# Feedback value per kind (ADR-029). Only the LATEST feedback per actor counts.
FEEDBACK_VALUE = {"useful": 1.0, "not_useful": -1.0, "outdated": -1.0, "wrong": -2.0}
FEEDBACK_MIN_DENOM = 3  # one vote cannot swing the score to the extreme


def latest_per_actor(rows: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """rows = (actor_id, feedback, created_at). Returns actor_id -> latest feedback kind.
    Ties on created_at are broken by input order (later row wins)."""
    best: dict[str, tuple[str, str]] = {}
    for actor_id, kind, at in rows:
        cur = best.get(actor_id)
        if cur is None or at >= cur[1]:
            best[actor_id] = (kind, at)
    return {a: k for a, (k, _) in best.items()}


def feedback_score(latest: dict[str, str]) -> float:
    """-1..1: sum of latest values / max(3, voters)."""
    if not latest:
        return 0.0
    total = sum(FEEDBACK_VALUE[k] for k in latest.values())
    return max(-1.0, min(1.0, total / max(FEEDBACK_MIN_DENOM, len(latest))))


def _age_days(ts: str, now: datetime) -> float:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return max(0.0, (now - t).total_seconds() / 86400.0)


def decay_score(
    importance: float, last_activity: str, now: datetime, half_life_days: float, fb: float
) -> tuple[float, float]:
    """(score 0..1, age_days). score = importance * 2^(-age/half_life) + 0.2 * feedback."""
    age = _age_days(last_activity, now)
    base = importance * math.pow(0.5, age / half_life_days)
    return max(0.0, min(1.0, base + 0.2 * fb)), age


@dataclass(frozen=True)
class SpineWeights:
    confirmed: float = 0.25
    sources: float = 0.15
    feedback: float = 0.20
    importance: float = 0.20
    freshness: float = 0.20

    def __post_init__(self) -> None:
        vals = self.as_dict().values()
        if any(v < 0 or v > 1 for v in vals):
            raise ValueError("spine weights must be within 0..1")
        if not math.isclose(sum(vals), 1.0, abs_tol=1e-9):
            raise ValueError("spine weights must sum to 1.0")

    def as_dict(self) -> dict[str, float]:
        return {
            "confirmed": self.confirmed,
            "sources": self.sources,
            "feedback": self.feedback,
            "importance": self.importance,
            "freshness": self.freshness,
        }


def parse_spine_weights(raw: str | None) -> SpineWeights:
    """AME_SPINE_WEIGHTS="confirmed=0.3,sources=0.1,..." (missing keys keep defaults)."""
    if not raw:
        return SpineWeights()
    vals = SpineWeights().as_dict()
    for part in raw.split(","):
        if not part.strip():
            continue
        k, sep, v = part.partition("=")
        k = k.strip()
        if not sep or k not in vals:
            raise ValueError(f"unknown spine weight {k!r}")
        vals[k] = float(v)
    return SpineWeights(**vals)


def spine_components(
    *,
    user_created: bool,
    n_sources: int,
    fb: float,
    importance: float,
    freshness: float,
) -> dict[str, float]:
    return {
        "confirmed": 1.0 if user_created else 0.7,
        "sources": min(1.0, n_sources / 3.0),
        "feedback": (fb + 1.0) / 2.0,
        "importance": importance,
        "freshness": freshness,
    }


def weighted(components: dict[str, float], w: SpineWeights) -> float:
    return sum(components[k] * v for k, v in w.as_dict().items())


# ---------- distill (ADR-032) ----------
def char_bigrams(text: str) -> set[str]:
    """Character bigrams of the text with whitespace removed (works for CJK and Latin)."""
    s = "".join(text.split()).casefold()
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def clusters(ids: Sequence[str], grams: dict[str, set[str]], threshold: float) -> list[list[str]]:
    """Union-find over pairs with jaccard >= threshold. Deterministic (sorted ids)."""
    order = sorted(ids)
    parent = {i: i for i in order}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(order):
        for b in order[i + 1 :]:
            if jaccard(grams[a], grams[b]) >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, list[str]] = {}
    for mid in order:
        groups.setdefault(find(mid), []).append(mid)
    return sorted((g for g in groups.values() if len(g) >= 2), key=lambda g: g[0])

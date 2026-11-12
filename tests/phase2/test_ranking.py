"""Step 2: pure ranking functions (ADR-020, Q17)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ame.config import Settings
from ame.domain.ranking import (
    Components,
    Weights,
    freshness_score,
    graph_score,
    keyword_score,
    query_terms,
    total_score,
    weights_used,
)


def test_default_weights_match_api_spec_and_sum_to_one() -> None:
    w = Weights()
    assert (w.vector, w.graph, w.importance, w.confidence, w.freshness) == (
        0.35, 0.20, 0.15, 0.20, 0.10
    )  # fmt: skip


@pytest.mark.parametrize("bad", [{"vector": 0.5}, {"graph": -0.1, "vector": 0.65}])
def test_invalid_weights_rejected(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        Weights(**bad)


def test_settings_reject_bad_weights_and_cloud_provider(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, search_weight_vector=0.9)
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, embedding_provider="openai")


def test_lexical_mode_keyword_takes_vector_weight_no_fake_vector() -> None:
    c = Components(keyword=0.5, vector=None, graph=0.0, importance=1, confidence=1, freshness=1)
    w = Weights()
    assert total_score(c, w) == pytest.approx(0.35 * 0.5 + 0.15 + 0.20 + 0.10)
    used = weights_used(c, w)
    assert used["vector"] == 0.0 and used["keyword"] == 0.35
    assert sum(used.values()) == pytest.approx(1.0)


def test_hybrid_uses_vector_weight() -> None:
    c = Components(keyword=1.0, vector=0.2, graph=0, importance=0, confidence=0, freshness=0)
    assert total_score(c, Weights()) == pytest.approx(0.07)
    assert weights_used(c, Weights())["keyword"] == 0.0


def test_components() -> None:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    assert freshness_score("2026-11-03T00:00:00Z", now) == 1.0
    assert freshness_score("2026-10-04T00:00:00Z", now) == pytest.approx(0.5)
    assert freshness_score("2027-01-01T00:00:00Z", now) == 1.0
    assert graph_score(0) == 0 and graph_score(1) == pytest.approx(1 / 3) and graph_score(9) == 1
    assert keyword_score(1, 2) == 0.5 and keyword_score(0, 0) == 0.0
    assert query_terms("a  A b") == ["a", "b"]
    assert len(query_terms(" ".join(f"t{i}" for i in range(40)))) == 16

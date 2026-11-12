"""Acceptance E (rebuild recall) and F (degrade) with the test-only hashing provider."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from ame.application.index_worker import IndexWorker
from ame.application.memory_service import MemoryService
from ame.application.schemas import SearchRequest, VersionCreate
from ame.application.search_service import SearchService, VectorBackend
from ame.infrastructure.unit_of_work import make_session_factory
from ame.infrastructure.vector_stores import InMemoryVectorStore, LanceDBVectorStore, cosine
from tests.conftest import USER, make
from tests.phase2.conftest import NOW
from tests.phase2.fakes import BrokenProvider, HashingEmbeddingProvider


def setup(engine: Engine, store: object) -> tuple[SearchService, IndexWorker]:
    sf = make_session_factory(engine)
    b = VectorBackend(HashingEmbeddingProvider(), store)  # type: ignore[arg-type]
    return SearchService(sf, vector=b), IndexWorker(sf, b)


def test_cosine_bounds() -> None:
    assert cosine([1, 0], [1, 0]) == 1.0 and cosine([1, 0], [-1, 0]) == 0.0
    assert cosine([0, 0], [1, 0]) == 0.0


def test_hybrid_scores_are_real_and_rebuild_recalls(svc: MemoryService, engine: Engine) -> None:
    store = InMemoryVectorStore()
    search, w = setup(engine, store)
    a = svc.create_memory(make(content="使用 uv 管理 python 依赖"), USER).memory_id
    svc.create_memory(make(content="周末去爬山"), USER)
    w.run_until_idle()
    before = search.search(SearchRequest(query="python 依赖管理"), NOW)
    assert before.retrieval_mode == "hybrid" and before.results[0].memory.memory_id == a
    top = before.results[0].score_breakdown
    assert top.vector is not None and 0 < top.vector <= 1 and top.vector_used
    w.rebuild()
    assert store.count() == 0
    w.run_until_idle()
    after = search.search(SearchRequest(query="python 依赖管理"), NOW)
    assert [r.memory.memory_id for r in after.results] == [
        r.memory.memory_id for r in before.results
    ]
    assert [r.score for r in after.results] == [r.score for r in before.results]


def test_stale_vector_is_not_used(svc: MemoryService, engine: Engine) -> None:
    store = InMemoryVectorStore()
    search, w = setup(engine, store)
    m = svc.create_memory(make(content="alpha beta"), USER).memory_id
    w.run_until_idle()
    svc.create_version(
        m, VersionCreate(content="gamma delta", change_reason="edit", expected_version=1), USER
    )
    r = search.search(SearchRequest(query="alpha beta"), NOW)
    assert r.results == []  # old vector only, no keyword match: dropped, not faked
    w.run_until_idle()
    assert search.search(SearchRequest(query="gamma delta"), NOW).results[0].score_breakdown.vector


def test_broken_provider_degrades(svc: MemoryService, engine: Engine) -> None:
    """Acceptance F: vector side broken -> lexical_graph, vector=None, search still works."""
    sf = make_session_factory(engine)
    svc.create_memory(make(content="degrade keyword"), USER)
    s = SearchService(sf, vector=VectorBackend(BrokenProvider(), InMemoryVectorStore()))
    r = s.search(SearchRequest(query="keyword"), NOW)
    assert r.retrieval_mode == "lexical_graph" and r.degraded_reason
    assert "VectorUnavailableError" in r.degraded_reason
    assert r.results and r.results[0].score_breakdown.vector is None
    w = IndexWorker(sf, VectorBackend(BrokenProvider(), InMemoryVectorStore()))
    st = w.run_once()
    assert st.retried == 1 and st.errors == ["VectorUnavailableError"]


def test_lancedb_missing_is_unavailable(tmp_path: Path) -> None:
    try:
        import lancedb  # noqa: F401
    except ImportError:
        from ame.application.ports import VectorUnavailableError

        with pytest.raises(VectorUnavailableError):
            LanceDBVectorStore(tmp_path / "v", 8)
    else:
        pytest.skip("lancedb installed; covered by test_lancedb_roundtrip")


def test_lancedb_roundtrip(svc: MemoryService, engine: Engine, tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    store = LanceDBVectorStore(tmp_path / "vec", HashingEmbeddingProvider().dimension)
    search, w = setup(engine, store)
    a = svc.create_memory(make(content="lance secret text"), USER).memory_id
    w.run_until_idle()
    assert store.count() == 1
    files = b"".join(p.read_bytes() for p in (tmp_path / "vec").rglob("*") if p.is_file())
    assert b"secret" not in files  # no memory text in the derived index
    r = search.search(SearchRequest(query="lance secret"), NOW)
    assert r.retrieval_mode == "hybrid" and r.results[0].memory.memory_id == a
    w.rebuild()
    assert store.count() == 0
    w.run_until_idle()
    assert store.count() == 1

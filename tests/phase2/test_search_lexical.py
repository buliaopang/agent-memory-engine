"""Step 2: lexical search + ranking through SearchService (P2-5, P2-6, ACCEPTANCE F)."""

from __future__ import annotations

import pytest

from ame.application.memory_service import MemoryService
from ame.application.schemas import EdgeCreate, RelationCreate, SearchRequest, StatusChange
from ame.application.search_service import SearchService
from ame.domain.enums import EdgeType, MemoryStatus, RelationType
from ame.domain.ranking import Weights
from tests.conftest import USER, make
from tests.phase2.conftest import NOW


def q(text: str, **kw: object) -> SearchRequest:
    return SearchRequest.model_validate({"query": text, **kw})


def ids(resp) -> list[str]:  # type: ignore[no-untyped-def]
    return [r.memory.memory_id for r in resp.results]


@pytest.fixture()
def corpus(svc: MemoryService) -> dict[str, str]:
    m = {
        "desk": svc.create_memory(make(content="此前验证过的桌面自动化流程", tags=["rpa"]), USER),
        "eng": svc.create_memory(make(content="Desktop Automation checklist"), USER),
        "other": svc.create_memory(make(content="完全无关的内容"), USER),
    }
    return {k: v.memory_id for k, v in m.items()}


def test_chinese_short_and_long_terms(search: SearchService, corpus: dict[str, str]) -> None:
    assert ids(search.search(q("流程"), NOW)) == [corpus["desk"]]  # 2 chars -> LIKE fallback
    assert ids(search.search(q("自动化"), NOW)) == [corpus["desk"]]  # >=3 chars -> FTS5
    assert ids(search.search(q("desktop"), NOW)) == [corpus["eng"]]  # case-insensitive
    assert ids(search.search(q("rpa"), NOW)) == [corpus["desk"]]  # tags are searchable
    assert ids(search.search(q("不存在的词"), NOW)) == []


def test_like_wildcards_are_literal(search: SearchService, corpus: dict[str, str]) -> None:
    assert ids(search.search(q("%"), NOW)) == []
    assert ids(search.search(q("_"), NOW)) == []


def test_lexical_mode_breakdown_is_truthful(search: SearchService, corpus: dict[str, str]) -> None:
    resp = search.search(q("自动化 checklist"), NOW)
    assert resp.retrieval_mode == "lexical_graph"
    assert resp.degraded_reason == "no embedding provider configured"
    assert len(resp.results) == 2
    for r in resp.results:
        b = r.score_breakdown
        assert b.vector is None and b.vector_used is False
        assert b.keyword == 0.5 and r.matched_terms == 1
        assert sum(b.weights.values()) == pytest.approx(1.0)
        expect = 0.35 * b.keyword + 0.2 * b.graph + 0.15 * b.importance
        expect += 0.2 * b.confidence + 0.1 * b.freshness
        assert r.score == pytest.approx(expect, abs=1e-6)


def test_order_is_deterministic_and_explained(svc: MemoryService, search: SearchService) -> None:
    hi = svc.create_memory(make(content="规则 alpha", importance=0.9), USER).memory_id
    lo = svc.create_memory(make(content="规则 alpha", importance=0.1), USER).memory_id
    first = ids(search.search(q("alpha"), NOW))
    assert first == [hi, lo] and ids(search.search(q("alpha"), NOW)) == first
    assert ids(search.search(q("alpha", top_k=1), NOW)) == [hi]


def test_hidden_statuses_and_conflicts(svc: MemoryService, search: SearchService) -> None:
    a = svc.create_memory(make(content="冲突甲 kiwi"), USER).memory_id
    b = svc.create_memory(make(content="冲突乙 kiwi"), USER).memory_id
    arch = svc.create_memory(make(content="归档 kiwi"), USER).memory_id
    svc.change_status(arch, StatusChange(to_status=MemoryStatus.ARCHIVED, reason="r"), USER)
    svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type=RelationType.CONTRADICTS),
        USER,
    )
    assert svc.get_memory(a).status == MemoryStatus.CONFLICTED
    assert ids(search.search(q("kiwi"), NOW)) == []
    got = search.search(q("kiwi", include_conflicts=True), NOW)
    assert sorted(ids(got)) == sorted([a, b]) and all(r.conflicted for r in got.results)
    assert arch not in ids(got)


def test_filters_apply_before_recall(svc: MemoryService, search: SearchService) -> None:
    svc.create_memory(make(content="mango L2"), USER)
    l3 = svc.create_memory(make(content="mango L3", layer="L3", memory_type="procedural"), USER)
    r = search.search(q("mango", layers=["L3"]), NOW)
    assert ids(r) == [l3.memory_id] and r.total_candidates == 1
    assert ids(search.search(q("mango", memory_types=["procedural"]), NOW)) == [l3.memory_id]
    assert ids(search.search(q("mango", scope_ids=["project:none"]), NOW)) == []


def test_graph_score_and_context(svc: MemoryService, search: SearchService) -> None:
    p = svc.create_memory(make(content="papaya parent"), USER).memory_id
    c = svc.create_memory(make(content="papaya child", parent_ids=[p]), USER).memory_id
    lone = svc.create_memory(make(content="papaya lone"), USER).memory_id
    resp = search.search(q("papaya", include_graph_context=True), NOW)
    r = {x.memory.memory_id: x for x in resp.results}
    assert r[p].score_breakdown.graph == pytest.approx(1 / 3)
    assert r[c].score_breakdown.graph == pytest.approx(1 / 3)
    assert r[lone].score_breakdown.graph == 0
    assert [(g.memory_id, g.link_type, g.direction) for g in r[c].graph_context] == [
        (p, "PARENT", "in")
    ]
    # a REFERENCE to a hit raises the graph score of both ends
    svc.create_edge(
        EdgeCreate(from_memory_id=lone, to_memory_id=p, edge_type=EdgeType.REFERENCE), USER
    )
    r2 = {x.memory.memory_id: x for x in search.search(q("papaya"), NOW).results}
    assert r2[p].score_breakdown.graph == pytest.approx(2 / 3)
    assert r2[lone].score_breakdown.graph == pytest.approx(1 / 3)


def test_include_sources_false_strips_sources(svc: MemoryService, search: SearchService) -> None:
    svc.create_memory(
        make(content="guava", sources=[{"source_type": "manual", "source_ref": "x"}]), USER
    )
    assert search.search(q("guava"), NOW).results[0].memory.sources
    assert search.search(q("guava", include_sources=False), NOW).results[0].memory.sources == []


def test_custom_weights(svc: MemoryService, engine) -> None:  # type: ignore[no-untyped-def]
    from ame.infrastructure.unit_of_work import make_session_factory

    svc.create_memory(make(content="lemon", importance=1.0, confidence=0.0), USER)
    w = Weights(vector=0.0, graph=0.0, importance=1.0, confidence=0.0, freshness=0.0)
    r = SearchService(make_session_factory(engine), w).search(q("lemon"), NOW)
    assert r.results[0].score == pytest.approx(1.0)

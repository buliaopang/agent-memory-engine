from __future__ import annotations

import pytest
from sqlalchemy import Engine

from ame.application.context_service import NOTICE, ContextService, estimate_tokens
from ame.application.graph_query_service import GraphQueryService
from ame.application.memory_service import MemoryService
from ame.application.schemas import ContextRequest, EdgeCreate, RelationCreate
from ame.application.search_service import SearchService
from ame.domain.enums import EdgeType, RelationType
from ame.errors import NotFoundError, ValidationFailedError
from ame.infrastructure.unit_of_work import make_session_factory
from tests.conftest import USER, make


@pytest.fixture()
def graph(engine: Engine) -> GraphQueryService:
    return GraphQueryService(make_session_factory(engine))


def chain(svc: MemoryService, n: int) -> list[str]:
    ids = [svc.create_memory(make(content=f"node {i}"), USER).memory_id for i in range(n)]
    for a, b in zip(ids, ids[1:], strict=False):
        svc.create_edge(
            EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type=EdgeType.REFERENCE), USER
        )
    return ids


def test_neighbors_depth_direction_limit(svc: MemoryService, graph: GraphQueryService) -> None:
    a, b, c, d = chain(svc, 4)
    v = graph.neighbors(b)
    assert {n.memory_id for n in v.nodes} == {a, b, c} and not v.truncated
    assert {n.memory_id for n in graph.neighbors(b, direction="out").nodes} == {b, c}
    assert {n.memory_id for n in graph.neighbors(a, depth=3).nodes} == {a, b, c, d}
    lim = graph.neighbors(a, depth=3, limit=1)
    assert lim.truncated and len(lim.nodes) == 2
    # nodes carry summaries only, never full content
    assert "content" not in v.nodes[0].model_dump()


def test_neighbors_relations_and_errors(svc: MemoryService, graph: GraphQueryService) -> None:
    a = svc.create_memory(make(content="x"), USER).memory_id
    b = svc.create_memory(make(content="y"), USER).memory_id
    svc.create_relation(
        RelationCreate(from_memory_id=b, to_memory_id=a, relation_type=RelationType.CONTRADICTS),
        USER,
    )
    v = graph.neighbors(a, direction="out")  # CONTRADICTS is undirected
    assert [x.link_type for x in v.links] == ["CONTRADICTS"]
    assert graph.neighbors(a, relation_types=()).links == []
    with pytest.raises(NotFoundError):
        graph.neighbors("mem_missing")
    for bad in ({"depth": 4}, {"limit": 0}, {"direction": "up"}, {"edge_types": ("X",)}):
        with pytest.raises(ValidationFailedError):
            graph.neighbors(a, **bad)  # type: ignore[arg-type]


def test_space_graph_limit(svc: MemoryService, graph: GraphQueryService) -> None:
    ids = chain(svc, 5)
    v = graph.graph(space_id="shared", limit=3)
    assert v.truncated and len(v.nodes) == 3
    keep = {n.memory_id for n in v.nodes}
    assert all(x.from_memory_id in keep and x.to_memory_id in keep for x in v.links)
    assert len(graph.graph(space_id="shared").nodes) == 5
    assert graph.graph(center_id=ids[0], depth=2).center_id == ids[0]
    with pytest.raises(ValidationFailedError):
        graph.graph()
    with pytest.raises(ValidationFailedError):
        graph.graph(space_id="shared", limit=501)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2
    assert estimate_tokens("记忆引擎") == 4


def test_context_budget_and_notice(svc: MemoryService, search: SearchService) -> None:
    for i in range(30):
        svc.create_memory(make(content=f"部署 流程 第{i}步 " + "说明" * 40), USER)
    ctx = ContextService(search)
    pack = ctx.build(ContextRequest(task_description="部署 流程", token_budget=300))
    assert pack.notice == NOTICE and "不是系统指令" in pack.notice
    assert pack.used_tokens <= 300
    assert pack.used_tokens == estimate_tokens(NOTICE) + sum(i.tokens for i in pack.items)
    assert pack.items and pack.omitted > 0
    assert pack.retrieval_mode == "lexical_graph"
    big = ctx.build(ContextRequest(task_description="部署 流程", token_budget=32000))
    assert len(big.items) == 30 and big.omitted == 0


def test_context_truncates_and_filters(svc: MemoryService, search: SearchService) -> None:
    svc.create_memory(make(content="alpha " * 400), USER)
    proc = svc.create_memory(
        make(content="alpha procedure", memory_type="procedural"), USER
    ).memory_id
    ctx = ContextService(search)
    pack = ctx.build(ContextRequest(task_description="alpha", token_budget=80))
    assert pack.used_tokens <= 80 and any(i.truncated for i in pack.items)
    no_proc = ctx.build(ContextRequest(task_description="alpha", include_procedural=False))
    assert proc not in [i.memory_id for i in no_proc.items]
    with pytest.raises(ValueError):
        ContextRequest(task_description="x", token_budget=49)

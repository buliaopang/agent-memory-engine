import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ame.application.schemas import EdgeCreate, RelationCreate
from ame.errors import CycleDetectedError
from tests.conftest import AGENT, USER, make


def _edge(a, b, t="PARENT"):
    return EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type=t)


def test_contradicts_does_not_trigger_cycle_detection_and_is_normalized(svc, engine):
    a = svc.create_memory(make(content="A"), AGENT).memory_id
    b = svc.create_memory(make(content="B"), AGENT).memory_id
    svc.create_edge(_edge(a, b), USER)  # structural A->B exists
    r1 = svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type="CONTRADICTS"), USER
    )
    r2 = svc.create_relation(  # reverse direction: would be a cycle if it were a DAG edge
        RelationCreate(from_memory_id=b, to_memory_id=a, relation_type="CONTRADICTS"), USER
    )
    r3 = svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type="CONTRADICTS"), USER
    )
    assert r1.created and not r2.created and not r3.created
    assert r1.relation_id == r2.relation_id == r3.relation_id
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM memory_relations")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM memory_edges")).scalar() == 1
    ra, rb = svc.list_relations(a), svc.list_relations(b)
    lo, hi = sorted((a, b))
    assert [(x.from_memory_id, x.to_memory_id, x.directed) for x in ra] == [(lo, hi, False)]
    assert [x.relation_id for x in rb] == [r1.relation_id]  # visible from both ends


def test_structural_cycle_a_b_c_a_rejected(svc, engine):
    a, b, c = (svc.create_memory(make(content=n), AGENT).memory_id for n in "ABC")
    svc.create_edge(_edge(a, b), USER)
    svc.create_edge(_edge(b, c), USER)
    with pytest.raises(CycleDetectedError):
        svc.create_edge(_edge(c, a), USER)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM memory_edges")).scalar() == 2


def test_cycle_detection_spans_all_structural_edge_types(svc):
    a, b, c = (svc.create_memory(make(content=n), AGENT).memory_id for n in "ABC")
    svc.create_edge(_edge(a, b, "PARENT"), USER)
    svc.create_edge(_edge(b, c, "REFERENCE"), USER)
    with pytest.raises(CycleDetectedError):
        svc.create_edge(_edge(c, a, "DERIVED_FROM"), USER)


def test_self_loop_rejected(svc):
    a = svc.create_memory(make(), AGENT).memory_id
    with pytest.raises(CycleDetectedError):
        svc.create_edge(_edge(a, a), USER)


def test_db_rejects_unnormalized_relation(svc, engine):
    a = svc.create_memory(make(), AGENT).memory_id
    b = svc.create_memory(make(), AGENT).memory_id
    hi, lo = max(a, b), min(a, b)
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO memory_relations (relation_id, relation_type, from_memory_id, "
                "to_memory_id, status, created_by, created_at, metadata_json) VALUES "
                "('r','CONTRADICTS',:hi,:lo,'active','x','2026-01-01T00:00:00Z','{}')"
            ),
            {"hi": hi, "lo": lo},
        )


@pytest.mark.parametrize("t", ["CONTRADICTS", "SUPPORTS", "RELATED_TO"])
def test_semantic_types_not_allowed_as_structural_edge(t):
    with pytest.raises(ValueError):
        EdgeCreate(from_memory_id="a", to_memory_id="b", edge_type=t)

"""Acceptance tests for ADR-006 .. ADR-009 (Q11..Q14)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from ame.application.schemas import EdgeCreate, LinkRevoke, RelationCreate, StatusChange
from ame.errors import (
    CycleDetectedError,
    InvalidTransitionError,
    MemoryArchivedError,
    ValidationFailedError,
)
from tests.conftest import AGENT, USER, make


def _active(svc):
    return svc.create_memory(make(), USER).memory_id


def _supersede(svc, old, new):
    sc = StatusChange(to_status="superseded", reason="newer", superseded_by_memory_id=new)
    return svc.change_status(old, sc, USER)


def _revert(svc, old):
    return svc.change_status(old, StatusChange(to_status="active", reason="mistake"), USER)


# ---------- ADR-006 (Q11) ----------
def test_1_revoked_supersedes_history_kept(svc, engine):
    old, new = _active(svc), _active(svc)
    _supersede(svc, old, new)
    _revert(svc, old)
    assert svc.list_edges(old) == []  # not in effective graph
    hist = svc.list_edges(old, include_revoked=True)
    assert len(hist) == 1
    e = hist[0]
    assert (e.edge_type, e.status, e.from_memory_id, e.to_memory_id) == (
        "SUPERSEDES", "revoked", new, old,
    )  # fmt: skip
    assert e.revoked_at and e.revoked_by == USER.actor_id
    assert e.revoked_actor_type == "user" and e.revoked_reason == "mistake"
    audit = svc.list_audit(old)[-1]
    assert (audit.after["trigger"], audit.reason, audit.actor_id) == (
        "T11",
        "mistake",
        USER.actor_id,
    )


def test_2_revoked_edge_ignored_by_cycle_detection(svc):
    a, b = _active(svc), _active(svc)
    _supersede(svc, b, a)  # a -SUPERSEDES-> b
    with pytest.raises(CycleDetectedError):  # b -> a would close a cycle while active
        svc.create_edge(
            EdgeCreate(from_memory_id=b, to_memory_id=a, edge_type="DERIVED_FROM"), USER
        )
    _revert(svc, b)  # revokes a -> b
    e = svc.create_edge(
        EdgeCreate(from_memory_id=b, to_memory_id=a, edge_type="DERIVED_FROM"), USER
    )
    assert e.created and e.status == "active"


def test_2b_revoked_non_supersedes_edge_ignored(svc):
    a, b = _active(svc), _active(svc)
    e = svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="PARENT"), USER)
    svc.revoke_edge(e.edge_id, LinkRevoke(reason="wrong parent"), AGENT)
    assert svc.get_memory(b).parent_ids == []
    back = svc.create_edge(EdgeCreate(from_memory_id=b, to_memory_id=a, edge_type="PARENT"), USER)
    assert back.created


def test_3_re_supersede_creates_new_edge_id(svc):
    old, new = _active(svc), _active(svc)
    _supersede(svc, old, new)
    _revert(svc, old)
    _supersede(svc, old, new)
    hist = svc.list_edges(old, include_revoked=True)
    assert len(hist) == 2 and hist[0].edge_id != hist[1].edge_id
    assert [h.status for h in hist] == ["revoked", "active"]


def test_supersedes_cannot_be_revoked_outside_state_machine(svc):
    old, new = _active(svc), _active(svc)
    _supersede(svc, old, new)
    edge_id = svc.list_edges(old)[0].edge_id
    with pytest.raises(ValidationFailedError):
        svc.revoke_edge(edge_id, LinkRevoke(reason="x"), USER)


def test_revoke_twice_rejected(svc):
    a, b = _active(svc), _active(svc)
    e = svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="REFERENCE"), USER)
    svc.revoke_edge(e.edge_id, LinkRevoke(reason="x"), USER)
    with pytest.raises(InvalidTransitionError):
        svc.revoke_edge(e.edge_id, LinkRevoke(reason="x"), USER)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM memory_edges",
        "UPDATE memory_edges SET status='active', revoked_at=NULL, revoked_by=NULL, "
        "revoked_actor_type=NULL, revoked_reason=NULL",
        "UPDATE memory_edges SET to_memory_id=from_memory_id",
    ],
)
def test_edge_history_immutable_at_db_level(svc, engine, sql):
    old, new = _active(svc), _active(svc)
    _supersede(svc, old, new)
    _revert(svc, old)
    with pytest.raises(DatabaseError), engine.begin() as c:
        c.execute(text(sql))


def test_db_partial_unique_only_for_active(svc, engine):
    a, b = _active(svc), _active(svc)
    svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="PARENT"), USER)
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO memory_edges (edge_id, from_memory_id, to_memory_id, edge_type, "
                "status, created_by, created_at, metadata_json) VALUES "
                "('dup', :a, :b, 'PARENT', 'active', 'x', '2026-01-01T00:00:00Z', '{}')"
            ),
            {"a": a, "b": b},
        )


# ---------- ADR-007 / ADR-008 (Q12 / Q13) ----------
def _rel(svc, a, b, t, actor=USER):
    return svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type=t), actor
    )


def test_4_supports_does_not_trigger_cycle_detection(svc, engine):
    a, b, c = (_active(svc) for _ in range(3))
    svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="PARENT"), USER)
    r1 = _rel(svc, a, b, "SUPPORTS")
    r2 = _rel(svc, b, c, "SUPPORTS")
    r3 = _rel(svc, c, a, "SUPPORTS")  # would be a cycle a->b->c->a if structural
    r4 = _rel(svc, b, a, "SUPPORTS")  # reverse of structural a->b
    assert all(r.created and r.directed for r in (r1, r2, r3, r4))
    assert (r4.from_memory_id, r4.to_memory_id) == (b, a)  # direction preserved
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM memory_edges")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM memory_relations")).scalar() == 4


def test_5_contradicts_does_not_trigger_cycle_detection(svc):
    a, b, c = (svc.create_memory(make(), AGENT).memory_id for _ in range(3))
    svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="PARENT"), USER)
    svc.create_edge(EdgeCreate(from_memory_id=b, to_memory_id=c, edge_type="PARENT"), USER)
    for x, y in ((a, b), (b, c), (c, a)):
        assert _rel(svc, x, y, "CONTRADICTS").created
    assert not _rel(svc, b, a, "CONTRADICTS").created  # normalized, undirected


def test_6_related_to_create_query_revoke(svc):
    a, b = _active(svc), _active(svc)
    r = _rel(svc, b, a, "RELATED_TO")
    assert r.created and not r.directed and r.status == "active"
    assert (r.from_memory_id, r.to_memory_id) == tuple(sorted((a, b)))
    assert [x.relation_id for x in svc.list_relations(a)] == [r.relation_id]
    assert [x.relation_id for x in svc.list_relations(b)] == [r.relation_id]
    assert svc.get_memory(a).status == "active"  # RELATED_TO never changes status
    out = svc.revoke_relation(r.relation_id, LinkRevoke(reason="not related"), AGENT)
    assert out.status == "revoked" and out.revoked_by == AGENT.actor_id
    assert out.revoked_reason == "not related" and out.revoked_at
    assert svc.list_relations(a) == []
    hist = svc.list_relations(a, include_revoked=True)
    assert [h.status for h in hist] == ["revoked"]
    again = _rel(svc, a, b, "RELATED_TO")
    assert again.created and again.relation_id != r.relation_id


def test_db_rejects_unknown_relation_type(svc, engine):
    a, b = sorted((_active(svc), _active(svc)))
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO memory_relations (relation_id, relation_type, from_memory_id, "
                "to_memory_id, status, created_by, created_at, metadata_json) VALUES "
                "('r', 'SIMILAR_TO', :a, :b, 'active', 'x', '2026-01-01T00:00:00Z', '{}')"
            ),
            {"a": a, "b": b},
        )


def test_db_rejects_supports_as_structural_edge(svc, engine):
    a, b = _active(svc), _active(svc)
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO memory_edges (edge_id, from_memory_id, to_memory_id, edge_type, "
                "status, created_by, created_at, metadata_json) VALUES "
                "('e', :a, :b, 'SUPPORTS', 'active', 'x', '2026-01-01T00:00:00Z', '{}')"
            ),
            {"a": a, "b": b},
        )


def test_similar_to_not_available():
    with pytest.raises(ValueError):
        RelationCreate(from_memory_id="a", to_memory_id="b", relation_type="SIMILAR_TO")


# ---------- ADR-009 (Q14) ----------
def _archive(svc, m):
    svc.change_status(m, StatusChange(to_status="archived", reason="old"), USER)


def test_7_archived_create_version_fails(svc):
    from ame.application.schemas import VersionCreate

    m = _active(svc)
    _archive(svc, m)
    with pytest.raises(MemoryArchivedError):
        svc.create_version(
            m, VersionCreate(content="v2", change_reason="r", expected_version=1), USER
        )
    assert len(svc.list_versions(m)) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO memory_versions (version_id, memory_id, version, content, content_hash, "
        "created_by, created_at) VALUES ('v', :m, 2, 'x', 'h', 'x', '2026-01-01T00:00:00Z')",
        "UPDATE memories SET current_version = 2 WHERE memory_id = :m",
    ],
)
def test_7b_archived_version_bypass_via_db_fails(svc, engine, sql):
    m = _active(svc)
    _archive(svc, m)
    with pytest.raises(DatabaseError, match="archived"), engine.begin() as c:
        c.execute(text(sql), {"m": m})


def test_8_restore_then_create_version_succeeds(svc):
    from ame.application.schemas import VersionCreate

    m = _active(svc)
    _archive(svc, m)
    svc.change_status(m, StatusChange(to_status="active", reason="needed again"), USER)
    ev = svc.list_audit(m)[-1]
    assert (ev.after["trigger"], ev.actor_id, ev.reason) == ("T10", USER.actor_id, "needed again")
    assert ev.event_type == "MEMORY_RESTORED" and ev.occurred_at
    v = svc.create_version(
        m, VersionCreate(content="v2", change_reason="r", expected_version=1), USER
    )
    assert v.version == 2 and svc.get_memory(m).current_version == 2

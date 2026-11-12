"""D: unified append-only audit_events (ADR-013)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ame.application.schemas import (
    ClassificationChange,
    EdgeCreate,
    LinkRevoke,
    RelationCreate,
    StatusChange,
    VersionCreate,
)
from ame.errors import CycleDetectedError
from tests.conftest import AGENT, USER, make

SECRET = "TOP-SECRET-MEMORY-BODY"


def _all_events(engine):
    with engine.connect() as c:
        return c.execute(
            text("SELECT seq, event_type, before_json, after_json, correlation_id, reason "
                 "FROM audit_events ORDER BY seq")
        ).fetchall()  # fmt: skip


def test_d1_every_operation_writes_its_event(svc, engine):
    a = svc.create_memory(make(content=SECRET), USER).memory_id
    b = svc.create_memory(make(content=SECRET), AGENT).memory_id
    svc.create_version(a, VersionCreate(expected_version=1, content=SECRET + "2",
                                        change_reason="fix"), USER)  # fmt: skip
    svc.change_classification(a, ClassificationChange(layer="L3"), USER)
    e = svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="PARENT"), USER)
    svc.revoke_edge(e.edge_id, LinkRevoke(reason="oops"), USER)
    r = svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type="RELATED_TO"), USER
    )
    svc.revoke_relation(r.relation_id, LinkRevoke(reason="nah"), USER)
    svc.change_status(a, StatusChange(to_status="archived", reason="old"), USER)
    svc.change_status(a, StatusChange(to_status="active", reason="back"), USER)
    types = [row[1] for row in _all_events(engine)]
    assert types == [
        "MEMORY_CREATED", "MEMORY_CREATED", "MEMORY_VERSION_CREATED", "MEMORY_UPDATED",
        "EDGE_CREATED", "EDGE_REVOKED", "RELATION_CREATED", "RELATION_REVOKED",
        "MEMORY_STATUS_CHANGED", "MEMORY_RESTORED",
    ]  # fmt: skip


def test_d2_audit_never_contains_content(svc, engine):
    a = svc.create_memory(make(content=SECRET), USER).memory_id
    svc.create_version(
        a, VersionCreate(expected_version=1, content=SECRET, change_reason="r"), USER
    )
    b = svc.create_memory(make(content=SECRET), USER).memory_id
    svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type="CONTRADICTS"), USER
    )
    rows = _all_events(engine)
    assert rows and all(SECRET not in str(row) for row in rows)
    assert any("content_hash" in (row[3] or "") for row in rows)


def test_d3_seq_is_gapless_and_unique(svc, engine):
    for _ in range(5):
        svc.create_memory(make(), USER)
    seqs = [row[0] for row in _all_events(engine)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_d4_one_correlation_id_per_use_case(svc, engine):
    a = svc.create_memory(make(), USER).memory_id
    b = svc.create_memory(make(), USER).memory_id
    svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type="CONTRADICTS"), USER
    )
    rows = _all_events(engine)[2:]
    assert [r[1] for r in rows] == [
        "RELATION_CREATED", "CONFLICT_OPENED", "MEMORY_STATUS_CHANGED", "MEMORY_STATUS_CHANGED",
    ]  # fmt: skip
    assert len({r[4] for r in rows}) == 1


def test_d5_failed_operation_writes_no_event(svc, engine):
    a = svc.create_memory(make(), USER).memory_id
    b = svc.create_memory(make(), USER).memory_id
    svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="PARENT"), USER)
    n = len(_all_events(engine))
    with pytest.raises(CycleDetectedError):
        svc.create_edge(EdgeCreate(from_memory_id=b, to_memory_id=a, edge_type="PARENT"), USER)
    assert len(_all_events(engine)) == n


def test_d6_list_audit_includes_related_events(svc):
    a = svc.create_memory(make(), USER).memory_id
    b = svc.create_memory(make(), USER).memory_id
    svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type="PARENT"), USER)
    evs = svc.list_audit(b)
    assert [e.event_type for e in evs] == ["MEMORY_CREATED", "EDGE_CREATED"]
    assert evs[1].related_memory_id == b and evs[1].before is None

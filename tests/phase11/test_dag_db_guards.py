"""A: DB-level DAG + endpoint guards (0004), IMMEDIATE writes, error mapping, Q-N11."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from ame.application.graph_service import GraphService
from ame.application.schemas import EdgeCreate, LinkRevoke, RelationCreate, StatusChange
from ame.errors import CycleDetectedError, LinkEndpointFrozenError, NotFoundError, StorageError
from ame.infrastructure.database import check_sqlite_version
from tests.conftest import AGENT, USER, make

EDGE = (
    "INSERT INTO memory_edges (edge_id, from_memory_id, to_memory_id, edge_type, status, "
    "created_by, created_at, metadata_json) VALUES (:i, :a, :b, :t, 'active', 'u', 't', '{}')"
)


def _m(svc) -> str:
    return svc.create_memory(make(), USER).memory_id


def _edge(svc, a, b, t="PARENT"):
    return svc.create_edge(EdgeCreate(from_memory_id=a, to_memory_id=b, edge_type=t), USER)


def test_a1_raw_sql_cycle_rejected_by_trigger(svc, engine):
    a, b, c = _m(svc), _m(svc), _m(svc)
    _edge(svc, a, b)
    _edge(svc, b, c, "REFERENCE")
    with pytest.raises(IntegrityError, match="DAG_CYCLE_DETECTED"), engine.begin() as conn:
        conn.execute(text(EDGE), {"i": "x", "a": c, "b": a, "t": "DERIVED_FROM"})


def test_a2_raw_self_loop_rejected(svc, engine):
    a = _m(svc)
    with pytest.raises(IntegrityError, match="DAG_CYCLE_DETECTED"), engine.begin() as conn:
        conn.execute(text(EDGE), {"i": "x", "a": a, "b": a, "t": "PARENT"})


def test_a3_revoked_edge_ignored_by_trigger(svc, engine):
    a, b = _m(svc), _m(svc)
    e = _edge(svc, a, b)
    svc.revoke_edge(e.edge_id, LinkRevoke(reason="wrong"), USER)
    with engine.begin() as conn:  # b->a is fine once a->b is revoked
        conn.execute(text(EDGE), {"i": "x", "a": b, "b": a, "t": "PARENT"})


def test_a4_update_cannot_reroute_into_cycle(svc, engine):
    a, b = _m(svc), _m(svc)
    _edge(svc, a, b)
    e2 = _edge(svc, b, _m(svc))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE memory_edges SET to_memory_id=:a WHERE edge_id=:e"),
                     {"a": a, "e": e2.edge_id})  # fmt: skip


def test_a5_endpoint_must_exist_even_with_fk_off(svc, engine):
    a = _m(svc)
    with pytest.raises(IntegrityError, match="EDGE_ENDPOINT_MISSING"), engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.execute(text(EDGE), {"i": "x", "a": a, "b": "ghost", "t": "PARENT"})


def test_a6_trigger_error_maps_to_domain_error_when_service_check_bypassed(svc, monkeypatch):
    a, b = _m(svc), _m(svc)
    _edge(svc, a, b)
    monkeypatch.setattr(GraphService, "_reachable", lambda *_a, **_k: False)
    with pytest.raises(CycleDetectedError) as ei:
        _edge(svc, b, a)
    assert ei.value.http_status == 409 and ei.value.details["db_guard"] == "DAG_CYCLE_DETECTED"


def test_a7_write_uow_uses_begin_immediate_reads_deferred(svc, engine):
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_c, _cur, stmt, *_a):  # type: ignore[no-untyped-def]
        if stmt.startswith("BEGIN"):
            seen.append(stmt)

    m = _m(svc)
    svc.get_memory(m)
    assert seen == ["BEGIN IMMEDIATE", "BEGIN"]


def test_a8_concurrent_opposite_edges_never_form_a_cycle(svc):
    a, b = _m(svc), _m(svc)
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def go(x: str, y: str) -> None:
        barrier.wait()
        try:
            _edge(svc, x, y)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=p) for p in ((a, b), (b, a))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(errors) == 1 and isinstance(errors[0], CycleDetectedError)
    assert len(svc.list_edges(a)) == 1


def test_a9_sqlite_version_floor():
    with pytest.raises(StorageError):
        check_sqlite_version((3, 34, 1))
    check_sqlite_version((3, 35, 0))


@pytest.mark.parametrize("frozen", ["archived", "rejected"])
def test_n11_frozen_memories_get_no_new_links(svc, engine, frozen):
    if frozen == "archived":
        f = _m(svc)
        svc.change_status(f, StatusChange(to_status="archived", reason="old"), USER)
    else:
        f = svc.create_memory(make(), AGENT).memory_id
        svc.change_status(f, StatusChange(to_status="rejected", reason="no"), USER)
    other = _m(svc)
    with pytest.raises(LinkEndpointFrozenError):
        _edge(svc, other, f)
    with pytest.raises(LinkEndpointFrozenError):
        svc.create_relation(
            RelationCreate(from_memory_id=f, to_memory_id=other, relation_type="RELATED_TO"), USER
        )
    with pytest.raises(IntegrityError, match="LINK_ENDPOINT_FROZEN"), engine.begin() as conn:
        conn.execute(text(EDGE), {"i": "x", "a": other, "b": f, "t": "REFERENCE"})


def test_n11_revoking_existing_link_of_archived_memory_still_allowed(svc):
    a, b = _m(svc), _m(svc)
    e = _edge(svc, a, b)
    svc.change_status(b, StatusChange(to_status="archived", reason="old"), USER)
    assert svc.revoke_edge(e.edge_id, LinkRevoke(reason="cleanup"), USER).status == "revoked"


def test_missing_memory_still_404(svc):
    with pytest.raises(NotFoundError):
        _edge(svc, _m(svc), "ghost")

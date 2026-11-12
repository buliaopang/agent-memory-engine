from collections.abc import Callable

import pytest

from ame.application.memory_service import MemoryService
from ame.application.schemas import Actor, LinkRevoke, RelationCreate, StatusChange
from ame.domain.enums import MemoryStatus
from ame.domain.state_machine import RULES
from ame.errors import (
    ConflictEvidenceRequiredError,
    InvalidTransitionError,
    TransitionNotPermittedError,
)
from tests.conftest import AGENT, SYSTEM, USER, make

S = MemoryStatus


def _active(svc: MemoryService) -> str:
    return svc.create_memory(make(), USER).memory_id


def reach(svc: MemoryService, status: S) -> str:
    if status is S.CANDIDATE:
        return svc.create_memory(make(), AGENT).memory_id
    if status is S.ACTIVE:
        return _active(svc)
    if status is S.REJECTED:
        m = reach(svc, S.CANDIDATE)
        svc.change_status(m, StatusChange(to_status=S.REJECTED, reason="no"), USER)
        return m
    if status is S.SUPERSEDED:
        m = _active(svc)
        sc = StatusChange(to_status=S.SUPERSEDED, reason="r", superseded_by_memory_id=_active(svc))
        svc.change_status(m, sc, USER)
        return m
    if status is S.CONFLICTED:
        m = _active(svc)
        rc = RelationCreate(
            from_memory_id=m, to_memory_id=_active(svc), relation_type="CONTRADICTS"
        )
        svc.create_relation(rc, USER)
        return m
    if status is S.ARCHIVED:
        m = _active(svc)
        svc.change_status(m, StatusChange(to_status=S.ARCHIVED, reason="old"), USER)
        return m
    raise AssertionError(status)


def _pick_actor(rule) -> Actor:
    for a in (USER, AGENT, SYSTEM):
        if a.actor_type in rule.actors:
            return a
    raise AssertionError


def _reach_archived_from(svc: MemoryService, prev: S) -> str:
    m = reach(svc, prev)
    svc.change_status(m, StatusChange(to_status=S.ARCHIVED, reason="arch"), USER)
    return m


def status_ev(ev):
    """(event_type, before.status, after.status, trigger) of a unified audit event."""
    return (
        ev.event_type,
        (ev.before or {}).get("status"),
        (ev.after or {}).get("status"),
        (ev.after or {}).get("trigger"),
    )


def _clear_conflicts(svc: MemoryService, m: str) -> None:
    """Q-C1 flow: revoke the CONTRADICTS relation(s); memory stays conflicted for T6/T7."""
    for r in svc.list_relations(m):
        if r.relation_type == "CONTRADICTS":
            svc.revoke_relation(r.relation_id, LinkRevoke(reason="not a conflict"), USER)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: f"{r.code}-{r.from_status}-{r.to_status}")
def test_every_legal_transition_with_audit(svc, rule):
    if rule.code == "T10":
        m = _reach_archived_from(svc, rule.to_status)
    else:
        m = reach(svc, rule.from_status)
        if rule.code in ("T6", "T7"):
            _clear_conflicts(svc, m)
    actor = _pick_actor(rule)
    if rule.code == "T5":
        # ADR-012: T5 needs an open conflict record; a bare status change is refused by the
        # DB guard. The legal path (CONTRADICTS -> ConflictService) is tested below.
        with pytest.raises(ConflictEvidenceRequiredError):
            svc.change_status(m, StatusChange(to_status=S.CONFLICTED, reason="x"), actor)
        assert svc.get_memory(m).status == "active"
        other = _active(svc)
        svc.create_relation(
            RelationCreate(from_memory_id=m, to_memory_id=other, relation_type="CONTRADICTS"),
            AGENT,
        )
        ev = [e for e in svc.list_audit(m) if e.memory_id == m and e.entity_type == "memory"][-1]
        assert status_ev(ev) == ("MEMORY_STATUS_CHANGED", "active", "conflicted", "T5")
        assert ev.actor_type == "system"
        return
    sup = _active(svc) if rule.code in ("T7", "T8") else None
    out = svc.change_status(
        m,
        StatusChange(to_status=rule.to_status, reason="because", superseded_by_memory_id=sup),
        actor,
    )
    assert out.status == rule.to_status
    ev = svc.list_audit(m)[-1]
    expected_type = "MEMORY_RESTORED" if rule.code == "T10" else "MEMORY_STATUS_CHANGED"
    assert status_ev(ev) == (
        expected_type, rule.from_status.value, rule.to_status.value, rule.code,
    )  # fmt: skip
    assert "content" not in (ev.before or {}) and "content" not in (ev.after or {})
    assert ev.actor_type == actor.actor_type and ev.actor_id == actor.actor_id
    assert ev.reason == "because"


ILLEGAL = [
    (f, t) for f in S for t in S if (f, t) not in {(r.from_status, r.to_status) for r in RULES}
]


@pytest.mark.parametrize(("frm", "to"), ILLEGAL, ids=lambda x: str(x))
def test_every_illegal_transition_rejected_without_audit(svc, frm, to):
    m = reach(svc, frm)
    before = len(svc.list_audit(m))
    with pytest.raises(InvalidTransitionError):
        svc.change_status(m, StatusChange(to_status=to, reason="x"), USER)
    assert svc.get_memory(m).status == frm
    assert len(svc.list_audit(m)) == before


def test_restore_must_return_to_previous_status(svc):
    m = _reach_archived_from(svc, S.REJECTED)
    with pytest.raises(InvalidTransitionError):
        svc.change_status(m, StatusChange(to_status=S.ACTIVE, reason="x"), USER)


def test_agent_cannot_review_or_restore(svc):
    m = reach(svc, S.CANDIDATE)
    with pytest.raises(TransitionNotPermittedError):
        svc.change_status(m, StatusChange(to_status=S.ACTIVE, reason="x"), AGENT)
    a = reach(svc, S.ARCHIVED)
    with pytest.raises(TransitionNotPermittedError):
        svc.change_status(a, StatusChange(to_status=S.ACTIVE, reason="x"), AGENT)


def test_creation_audit_t1_t2(svc):
    u = svc.create_memory(make(), USER)
    a = svc.create_memory(make(), AGENT)
    assert u.status == "active" and svc.list_audit(u.memory_id)[0].after["trigger"] == "T2"
    assert a.status == "candidate" and svc.list_audit(a.memory_id)[0].after["trigger"] == "T1"


def test_both_active_contradicts_become_conflicted_by_system(svc):
    a, b = _active(svc), _active(svc)
    r = svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type="CONTRADICTS"), USER
    )
    assert r.review_notice == "both_marked_conflicted"
    for m in (a, b):
        assert svc.get_memory(m).status == "conflicted"
        ev = [e for e in svc.list_audit(m) if e.memory_id == m][-1]
        assert status_ev(ev)[3] == "T5" and ev.actor_type == "system"


def test_candidate_contradicts_active_only_records_relation(svc):
    c, a = reach(svc, S.CANDIDATE), _active(svc)
    r = svc.create_relation(
        RelationCreate(from_memory_id=c, to_memory_id=a, relation_type="CONTRADICTS"), USER
    )
    assert r.review_notice == "candidate_contradicts_active_review_required"
    assert svc.get_memory(c).status == "candidate" and svc.get_memory(a).status == "active"


def test_supersede_creates_supersedes_edge(svc, engine):
    from sqlalchemy import text

    old, new = _active(svc), _active(svc)
    sc = StatusChange(to_status=S.SUPERSEDED, reason="newer", superseded_by_memory_id=new)
    svc.change_status(old, sc, USER)
    with engine.connect() as c:
        row = c.execute(
            text("SELECT from_memory_id, to_memory_id, edge_type FROM memory_edges")
        ).one()
    assert tuple(row) == (new, old, "SUPERSEDES")


def test_supersede_without_target_rejected(svc):
    from ame.errors import ValidationFailedError

    m = _active(svc)
    with pytest.raises(ValidationFailedError):
        svc.change_status(m, StatusChange(to_status=S.SUPERSEDED, reason="x"), USER)


Builder = Callable[[MemoryService], str]

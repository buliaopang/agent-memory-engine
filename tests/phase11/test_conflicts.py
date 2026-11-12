"""B/C: ConflictService (ADR-012) - Q-C1..Q-C4, T6/T7 guard, resolution flows."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ame.application.schemas import ConflictResolve, LinkRevoke, RelationCreate, StatusChange
from ame.errors import (
    ConflictUnresolvedError,
    InvalidTransitionError,
    MemoryArchivedError,
    TransitionNotPermittedError,
)
from tests.conftest import AGENT, USER, make


def _m(svc, actor=USER) -> str:
    return svc.create_memory(make(content="secret-content-xyz"), actor).memory_id


def _contradict(svc, a, b, actor=USER):
    return svc.create_relation(
        RelationCreate(from_memory_id=a, to_memory_id=b, relation_type="CONTRADICTS"), actor
    )


def _res(kind: str, **ev: object) -> ConflictResolve:
    return ConflictResolve(resolution=kind, reason="decided", evidence=ev or {"note": "ok"})


def _status(svc, m) -> str:
    return svc.get_memory(m).status


def test_b1_both_active_opens_one_conflict_and_marks_both(svc):
    a, b = _m(svc), _m(svc)
    r = _contradict(svc, a, b)
    assert r.review_notice == "both_marked_conflicted"
    (c,) = svc.list_conflicts(a)
    assert c.status == "open" and c.relation_id == r.relation_id
    assert {c.memory_a_id, c.memory_b_id} == {a, b} and c.memory_a_id < c.memory_b_id
    assert c.evidence["relation_id"] == r.relation_id and "a_content_hash" in c.evidence
    assert _status(svc, a) == _status(svc, b) == "conflicted"


def test_b2_agent_may_declare_conflict(svc):
    a, b = _m(svc), _m(svc)
    assert _contradict(svc, a, b, AGENT).review_notice == "both_marked_conflicted"
    assert svc.list_conflicts(a)[0].opened_actor_type == "agent"


def test_b3_qc2_conflicted_vs_new_active_marks_active_side(svc):
    a, b, c = _m(svc), _m(svc), _m(svc)
    _contradict(svc, a, b)
    r = _contradict(svc, a, c)
    assert r.review_notice == "conflict_opened"
    assert _status(svc, c) == "conflicted"
    assert len(svc.list_conflicts(a)) == 2


def test_b4_candidate_vs_active_only_notice(svc):
    cand, act = _m(svc, AGENT), _m(svc)
    r = _contradict(svc, cand, act)
    assert r.review_notice == "candidate_contradicts_active_review_required"
    assert svc.list_conflicts(act) == [] and _status(svc, act) == "active"


def test_b5_duplicate_contradicts_is_idempotent(svc):
    a, b = _m(svc), _m(svc)
    _contradict(svc, a, b)
    again = _contradict(svc, b, a)
    assert again.created is False and len(svc.list_conflicts(a)) == 1


def test_b6_t6_t7_blocked_while_conflict_open(svc):
    a, b = _m(svc), _m(svc)
    _contradict(svc, a, b)
    with pytest.raises(ConflictUnresolvedError) as ei:
        svc.change_status(a, StatusChange(to_status="active", reason="x"), USER)
    assert ei.value.http_status == 409
    with pytest.raises(ConflictUnresolvedError):
        svc.change_status(
            a, StatusChange(to_status="superseded", reason="x", superseded_by_memory_id=b), USER
        )


def test_c1_revoking_last_contradiction_keeps_conflicted_and_prompts_t6(svc):
    a, b = _m(svc), _m(svc)
    r = _contradict(svc, a, b)
    out = svc.revoke_relation(r.relation_id, LinkRevoke(reason="misread"), USER)
    assert out.review_notice == "conflict_cleared_user_confirm_T6"
    assert _status(svc, a) == _status(svc, b) == "conflicted"
    (c,) = svc.list_conflicts(a, include_resolved=True)
    assert (c.status, c.resolution) == ("resolved", "relation_revoked")
    svc.change_status(a, StatusChange(to_status="active", reason="confirmed"), USER)
    assert _status(svc, a) == "active"


def test_c1b_revoking_one_of_two_conflicts_gives_no_t6_prompt(svc):
    a, b, c = _m(svc), _m(svc), _m(svc)
    r1 = _contradict(svc, a, b)
    _contradict(svc, a, c)
    out = svc.revoke_relation(r1.relation_id, LinkRevoke(reason="misread"), USER)
    assert _status(svc, a) == "conflicted"
    # b has no open conflict left -> prompt; a still has one open with c
    assert out.review_notice == "conflict_cleared_user_confirm_T6"
    assert len(svc.list_conflicts(a)) == 1


def test_c2_kept_both_keeps_relation_and_reactivates(svc):
    a, b = _m(svc), _m(svc)
    r = _contradict(svc, a, b)
    (c,) = svc.list_conflicts(a)
    out = svc.resolve_conflict(c.conflict_id, _res("kept_both", why="different scopes"), USER)
    assert out.status == "resolved" and out.resolution_evidence == {"why": "different scopes"}
    assert _status(svc, a) == _status(svc, b) == "active"
    rels = svc.list_relations(a)
    assert [x.relation_id for x in rels] == [r.relation_id] and rels[0].status == "active"


def test_c3_superseded_resolution(svc):
    a, b = _m(svc), _m(svc)
    _contradict(svc, a, b)
    (c,) = svc.list_conflicts(a)
    lo, hi = c.memory_a_id, c.memory_b_id
    svc.resolve_conflict(c.conflict_id, _res("a_superseded_by_b"), USER)
    assert _status(svc, lo) == "superseded" and _status(svc, hi) == "active"
    sup = [e for e in svc.list_edges(lo) if e.edge_type == "SUPERSEDES"]
    assert sup and sup[0].from_memory_id == hi


def test_c4_only_user_resolves_and_evidence_required(svc):
    a, b = _m(svc), _m(svc)
    _contradict(svc, a, b)
    (c,) = svc.list_conflicts(a)
    with pytest.raises(TransitionNotPermittedError):
        svc.resolve_conflict(c.conflict_id, _res("dismissed"), AGENT)
    from ame.errors import ConflictEvidenceRequiredError

    with pytest.raises(ConflictEvidenceRequiredError):
        svc.resolve_conflict(
            c.conflict_id, ConflictResolve(resolution="dismissed", reason="r", evidence={}), USER
        )
    with pytest.raises(ValueError):
        ConflictResolve(resolution="relation_revoked", reason="r", evidence={"x": 1})
    assert svc.list_conflicts(a)[0].status == "open"


def test_c5_resolve_twice_rejected(svc):
    a, b = _m(svc), _m(svc)
    _contradict(svc, a, b)
    (c,) = svc.list_conflicts(a)
    svc.resolve_conflict(c.conflict_id, _res("dismissed"), USER)
    with pytest.raises(InvalidTransitionError):
        svc.resolve_conflict(c.conflict_id, _res("dismissed"), USER)


def test_c6_archived_side_must_be_restored_first(svc):
    a, b = _m(svc), _m(svc)
    _contradict(svc, a, b)
    svc.change_status(a, StatusChange(to_status="archived", reason="later"), USER)
    (c,) = svc.list_conflicts(b)
    with pytest.raises(MemoryArchivedError):
        svc.resolve_conflict(c.conflict_id, _res("kept_both"), USER)
    svc.change_status(a, StatusChange(to_status="conflicted", reason="back"), USER)  # T10
    svc.resolve_conflict(c.conflict_id, _res("kept_both"), USER)
    assert _status(svc, a) == "active"


def test_c7_partial_resolution_keeps_other_conflict(svc):
    a, b, c = _m(svc), _m(svc), _m(svc)
    _contradict(svc, a, b)
    _contradict(svc, a, c)
    first = next(x for x in svc.list_conflicts(a) if b in (x.memory_a_id, x.memory_b_id))
    out = svc.resolve_conflict(first.conflict_id, _res("dismissed"), USER)
    assert out.notice == "other_conflicts_still_open"
    assert _status(svc, a) == "conflicted" and _status(svc, b) == "active"


def test_c8_db_forbids_conflict_delete_and_reopen(svc, engine):
    a, b = _m(svc), _m(svc)
    _contradict(svc, a, b)
    (c,) = svc.list_conflicts(a)
    svc.resolve_conflict(c.conflict_id, _res("dismissed"), USER)
    for sql in ("DELETE FROM memory_conflicts", "UPDATE memory_conflicts SET status='open'"):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(sql))


def test_c9_conflict_rows_need_active_contradicts(svc, engine):
    a, b = sorted((_m(svc), _m(svc)))
    with pytest.raises(IntegrityError, match="CONFLICT_EVIDENCE_REQUIRED"), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_conflicts (conflict_id, relation_id, memory_a_id, memory_b_id,"
                " status, opened_at, opened_by, opened_actor_type, evidence_json) VALUES "
                "('c', NULL, :a, :b, 'open', 't', 'u', 'user', '{}')"
            ),
            {"a": a, "b": b},
        )

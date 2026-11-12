"""ConflictService: substantive conflicts between memories (ADR-012).

- Agents and users may *declare* a conflict (create a CONTRADICTS relation); only a user
  may *resolve* one, with a reason and non-empty evidence (Q-C4).
- A conflict is opened when both sides are active/conflicted. Active sides move to
  conflicted (T5, system actor) - also when the other side was already conflicted (Q-C2).
- Revoking the CONTRADICTS relation closes the conflict as `relation_revoked` but never
  changes statuses: a memory left conflicted with no open conflict waits for the user to
  confirm T6 (Q-C1).
- `kept_both` / `dismissed` keep the CONTRADICTS relation (Q-C3).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ame.application import audit
from ame.application.ids import new_id, utc_now
from ame.application.lifecycle import LifecycleService
from ame.application.phase4_service import compute_consensus_in
from ame.application.relation_service import RelationService
from ame.application.schemas import Actor, ConflictRead, ConflictResolve, ConflictSuggestion
from ame.domain.conflux import suggest_conflict_winner
from ame.domain.enums import (
    ActorType,
    AuditEntityType,
    AuditEventType,
    ConflictResolution,
    MemoryStatus,
)
from ame.domain.phase4_scoring import SpineWeights
from ame.errors import (
    ConflictEvidenceRequiredError,
    InvalidTransitionError,
    MemoryArchivedError,
    NotFoundError,
    TransitionNotPermittedError,
)
from ame.infrastructure.models import Memory, MemoryConflict, MemoryRelation
from ame.infrastructure.unit_of_work import UnitOfWork

SYSTEM_ACTOR = Actor(actor_type=ActorType.SYSTEM, actor_id="system")
LIVE = frozenset({MemoryStatus.ACTIVE.value, MemoryStatus.CONFLICTED.value})
NOTICE_BOTH = "both_marked_conflicted"
NOTICE_OPENED = "conflict_opened"
NOTICE_CANDIDATE = "candidate_contradicts_active_review_required"
NOTICE_CONFIRM_T6 = "conflict_cleared_user_confirm_T6"


def conflict_read(
    c: MemoryConflict,
    notice: str | None = None,
    suggestion: ConflictSuggestion | None = None,
) -> ConflictRead:
    return ConflictRead.model_validate(
        {
            "conflict_id": c.conflict_id,
            "relation_id": c.relation_id,
            "memory_a_id": c.memory_a_id,
            "memory_b_id": c.memory_b_id,
            "status": c.status,
            "opened_at": c.opened_at,
            "opened_by": c.opened_by,
            "opened_actor_type": c.opened_actor_type,
            "evidence": json.loads(c.evidence_json),
            "resolved_at": c.resolved_at,
            "resolved_by": c.resolved_by,
            "resolved_actor_type": c.resolved_actor_type,
            "resolution": c.resolution,
            "resolution_reason": c.resolution_reason,
            "resolution_evidence": (
                json.loads(c.resolution_evidence_json) if c.resolution_evidence_json else None
            ),
            "notice": notice,
            "suggestion": suggestion,
        }
    )


_CONSENSUS_STATUSES = (
    MemoryStatus.ACTIVE.value,
    MemoryStatus.CONFLICTED.value,
    MemoryStatus.SUPERSEDED.value,
)
_DEFAULT_WEIGHTS = SpineWeights()


def suggest_for_conflicts(
    uow: UnitOfWork, conflicts: list[MemoryConflict]
) -> dict[str, ConflictSuggestion | None]:
    """Compute Conflux total-order conflict suggestions for ``conflicts``."""
    if not conflicts:
        return {}
    scopes: set[str] = set()
    extra_mems: dict[str, Memory] = {}
    for c in conflicts:
        for mid in (c.memory_a_id, c.memory_b_id):
            m = uow.memories.get(mid)
            if m is not None:
                scopes.add(m.scope_id)
                extra_mems[mid] = m
    if not scopes:
        return dict.fromkeys((c.conflict_id for c in conflicts), None)
    mems_by_id = {m.memory_id: m for m in uow.p4.memories(sorted(scopes), _CONSENSUS_STATUSES)}
    for mid, m in extra_mems.items():
        mems_by_id.setdefault(mid, m)
    _scored, _nsrc, consensus = compute_consensus_in(
        uow, list(mems_by_id.values()), _DEFAULT_WEIGHTS, 90.0, datetime.now(UTC)
    )
    out: dict[str, ConflictSuggestion | None] = {}
    for c in conflicts:
        raw = suggest_conflict_winner(c.memory_a_id, c.memory_b_id, consensus)
        if raw is None:
            out[c.conflict_id] = None
        else:
            out[c.conflict_id] = ConflictSuggestion(
                winner_memory_id=raw.winner_memory_id,
                loser_memory_id=raw.loser_memory_id,
                recommended_resolution=ConflictResolution(raw.recommended_resolution),
                winner_rank=raw.winner_rank,
                loser_rank=raw.loser_rank,
                winner_epoch=raw.winner_epoch,
                loser_epoch=raw.loser_epoch,
                winner_pivot_memory_id=raw.winner_pivot_memory_id,
                loser_pivot_memory_id=raw.loser_pivot_memory_id,
                explanation=raw.explanation,
            )
    return out


def _conflict_state(c: MemoryConflict) -> dict[str, Any]:
    return {"status": c.status, "relation_id": c.relation_id, "resolution": c.resolution}


class ConflictService:
    def __init__(self, lifecycle: LifecycleService, relations: RelationService) -> None:
        self._life = lifecycle
        self._relations = relations

    @staticmethod
    def _get(uow: UnitOfWork, memory_id: str) -> Memory:
        m = uow.memories.get(memory_id)
        if m is None:
            raise NotFoundError("Memory not found", {"memory_id": memory_id})
        return m

    def declare(self, uow: UnitOfWork, rel: MemoryRelation, actor: Actor) -> str | None:
        """Called right after a *new* CONTRADICTS relation was inserted."""
        a = self._get(uow, rel.from_memory_id)
        b = self._get(uow, rel.to_memory_id)
        statuses = {a.status, b.status}
        if not {a.status, b.status} <= LIVE:
            if MemoryStatus.CANDIDATE.value in statuses and statuses & LIVE:
                return NOTICE_CANDIDATE
            return None
        both_active = statuses == {MemoryStatus.ACTIVE.value}
        evidence: dict[str, Any] = {"relation_id": rel.relation_id, "declared_by": actor.actor_id}
        for side, m in (("a", a), ("b", b)):
            v = uow.versions.get(m.memory_id, m.current_version)
            evidence[f"{side}_version"] = m.current_version
            evidence[f"{side}_content_hash"] = v.content_hash if v else None
        c = MemoryConflict(
            conflict_id=new_id("cfl"),
            relation_id=rel.relation_id,
            memory_a_id=a.memory_id,
            memory_b_id=b.memory_id,
            status="open",
            opened_at=utc_now(),
            opened_by=actor.actor_id,
            opened_actor_type=actor.actor_type.value,
            evidence_json=json.dumps(evidence, sort_keys=True),
        )
        uow.conflicts.add(c)
        audit.record(
            uow, AuditEventType.CONFLICT_OPENED, AuditEntityType.CONFLICT, c.conflict_id, actor,
            "CONTRADICTS declared", memory_id=a.memory_id, related_memory_id=b.memory_id,
            after=_conflict_state(c),
        )  # fmt: skip
        for m in (a, b):
            if m.status == MemoryStatus.ACTIVE.value:
                self._life.set_status(
                    uow, m, MemoryStatus.CONFLICTED, SYSTEM_ACTOR,
                    f"conflict {c.conflict_id} (CONTRADICTS {rel.relation_id})",
                )  # fmt: skip
        return NOTICE_BOTH if both_active else NOTICE_OPENED

    def revoke_contradiction(
        self, uow: UnitOfWork, relation_id: str, actor: Actor, reason: str
    ) -> tuple[MemoryRelation, str | None]:
        rel = self._relations.revoke_relation(uow, relation_id, actor, reason)
        c = uow.conflicts.open_for_relation(rel.relation_id)
        if c is not None:
            self._close(uow, c, actor, ConflictResolution.RELATION_REVOKED, reason, {
                "relation_id": rel.relation_id, "revoked": True,
            })  # fmt: skip
        notice = None
        for mid in (rel.from_memory_id, rel.to_memory_id):
            m = self._get(uow, mid)
            if m.status == MemoryStatus.CONFLICTED.value and uow.conflicts.count_open(mid) == 0:
                notice = NOTICE_CONFIRM_T6
        return rel, notice

    def _close(
        self,
        uow: UnitOfWork,
        c: MemoryConflict,
        actor: Actor,
        resolution: ConflictResolution,
        reason: str,
        evidence: dict[str, Any],
    ) -> None:
        before = _conflict_state(c)
        uow.conflicts.resolve(
            c, actor_type=actor.actor_type.value, actor_id=actor.actor_id,
            resolution=resolution.value, reason=reason,
            evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True), now=utc_now(),
        )  # fmt: skip
        audit.record(
            uow, AuditEventType.CONFLICT_RESOLVED, AuditEntityType.CONFLICT, c.conflict_id,
            actor, reason, memory_id=c.memory_a_id, related_memory_id=c.memory_b_id,
            before=before, after=_conflict_state(c),
        )  # fmt: skip

    def resolve(
        self, uow: UnitOfWork, conflict_id: str, cmd: ConflictResolve, actor: Actor
    ) -> ConflictRead:
        if actor.actor_type is not ActorType.USER:
            raise TransitionNotPermittedError("Only a user may resolve a conflict (Q-C4)")
        if not cmd.evidence:
            raise ConflictEvidenceRequiredError("Resolving a conflict requires evidence")
        c = uow.conflicts.get(conflict_id)
        if c is None:
            raise NotFoundError("Conflict not found", {"conflict_id": conflict_id})
        if c.status != "open":
            raise InvalidTransitionError(
                "Conflict is already resolved", {"conflict_id": c.conflict_id}
            )
        a, b = self._get(uow, c.memory_a_id), self._get(uow, c.memory_b_id)
        for m in (a, b):
            if m.status == MemoryStatus.ARCHIVED.value:
                raise MemoryArchivedError(
                    "Restore the archived memory before resolving", {"memory_id": m.memory_id}
                )
        self._close(uow, c, actor, cmd.resolution, cmd.reason, cmd.evidence)
        r = cmd.resolution
        winners: tuple[Memory, ...] = (a, b)
        if r in (ConflictResolution.A_SUPERSEDED_BY_B, ConflictResolution.B_SUPERSEDED_BY_A):
            loser, winner = (a, b) if r is ConflictResolution.A_SUPERSEDED_BY_B else (b, a)
            self._life.set_status(
                uow, loser, MemoryStatus.SUPERSEDED, actor, cmd.reason, winner.memory_id,
                conflict_resolution=True,
            )  # fmt: skip
            winners = (winner,)
        notice = None
        for m in winners:
            if m.status == MemoryStatus.CONFLICTED.value:
                if uow.conflicts.count_open(m.memory_id) == 0:
                    self._life.set_status(uow, m, MemoryStatus.ACTIVE, actor, cmd.reason)
                else:
                    notice = "other_conflicts_still_open"
        sug = suggest_for_conflicts(uow, [c]).get(c.conflict_id)
        return conflict_read(c, notice, suggestion=sug)

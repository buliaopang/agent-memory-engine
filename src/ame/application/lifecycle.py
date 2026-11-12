"""LifecycleService: the single place where memories.status changes (ADR-003/012).

Shared by MemoryService (explicit status changes) and ConflictService (T5/T6/T7 driven by
conflicts) so authorization, supersession edges and auditing are identical everywhere.
"""

from __future__ import annotations

from ame.application import audit
from ame.application.graph_service import GraphService
from ame.application.ids import utc_now
from ame.application.schemas import Actor, EdgeCreate
from ame.domain.enums import AuditEntityType, AuditEventType, EdgeType, MemoryStatus
from ame.domain.state_machine import TransitionRule, authorize_transition
from ame.errors import ConflictUnresolvedError, InvalidTransitionError, ValidationFailedError
from ame.infrastructure.models import Memory
from ame.infrastructure.unit_of_work import UnitOfWork

CONFLICT_EXITS = frozenset({"T6", "T7"})


class LifecycleService:
    def __init__(self, graph: GraphService) -> None:
        self._graph = graph

    def set_status(
        self,
        uow: UnitOfWork,
        m: Memory,
        to_status: MemoryStatus,
        actor: Actor,
        reason: str,
        superseded_by: str | None = None,
        *,
        conflict_resolution: bool = False,
    ) -> TransitionRule:
        from_status = MemoryStatus(m.status)
        rule = authorize_transition(from_status, to_status, actor.actor_type)
        if rule.code in CONFLICT_EXITS and not conflict_resolution:
            n_open = uow.conflicts.count_open(m.memory_id)
            if n_open:
                raise ConflictUnresolvedError(
                    "Memory has open conflicts; resolve them via resolve_conflict first",
                    {"memory_id": m.memory_id, "open_conflicts": n_open},
                )
        if rule.code == "T10":
            prev = uow.audit.pre_archive_status(m.memory_id)
            if prev != to_status.value:
                raise InvalidTransitionError(
                    "Restore must return to the pre-archive status", {"expected": prev}
                )
        if to_status is MemoryStatus.SUPERSEDED and rule.code != "T10":
            if not superseded_by:
                raise ValidationFailedError("superseded_by_memory_id is required")
            self._graph.add_edge(
                uow,
                EdgeCreate(
                    from_memory_id=superseded_by,
                    to_memory_id=m.memory_id,
                    edge_type=EdgeType.SUPERSEDES,
                ),
                actor,
            )
        elif superseded_by is not None:
            # T10 restores the existing SUPERSEDES edge; it never creates a new one.
            raise ValidationFailedError("superseded_by_memory_id only valid for T7/T8")
        if rule.code == "T11":
            # ADR-006: revoke (never delete) the active SUPERSEDES edges pointing at this memory.
            for edge in uow.edges.active_incoming(m.memory_id, EdgeType.SUPERSEDES.value):
                self._graph.revoke_edge(uow, edge, actor, reason, via_state_machine=True)
        before = audit.memory_state(uow, m)
        m.status = to_status.value
        m.updated_at = utc_now()
        uow.memories.flush()
        after = {**audit.memory_state(uow, m), "trigger": rule.code}
        if superseded_by:
            after["superseded_by"] = superseded_by
        etype = (
            AuditEventType.MEMORY_RESTORED
            if rule.code == "T10"
            else AuditEventType.MEMORY_STATUS_CHANGED
        )
        audit.record(
            uow, etype,
            AuditEntityType.MEMORY, m.memory_id, actor, reason,
            memory_id=m.memory_id, related_memory_id=superseded_by, before=before, after=after,
        )  # fmt: skip
        return rule

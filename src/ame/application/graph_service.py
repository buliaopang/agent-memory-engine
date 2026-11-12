"""GraphService: Structural DAG edges only (ADR-001/006). Semantic relations: RelationService."""

from __future__ import annotations

import json
from collections import deque

from ame.application import audit
from ame.application.ids import new_id, utc_now
from ame.application.schemas import Actor, EdgeCreate, EdgeRead
from ame.domain.enums import (
    FROZEN_LINK_STATUSES,
    ActorType,
    AuditEntityType,
    AuditEventType,
    EdgeType,
)
from ame.errors import (
    CycleDetectedError,
    InvalidTransitionError,
    LinkEndpointFrozenError,
    NotFoundError,
    ParentAlreadyExistsError,
    TransitionNotPermittedError,
    ValidationFailedError,
)
from ame.infrastructure.models import MemoryEdge
from ame.infrastructure.unit_of_work import UnitOfWork

REVOKERS = frozenset({ActorType.USER, ActorType.AGENT})


def edge_read(e: MemoryEdge, created: bool = False) -> EdgeRead:
    return EdgeRead.model_validate(
        {
            "edge_id": e.edge_id,
            "from_memory_id": e.from_memory_id,
            "to_memory_id": e.to_memory_id,
            "edge_type": e.edge_type,
            "status": e.status,
            "created_by": e.created_by,
            "created_at": e.created_at,
            "metadata": json.loads(e.metadata_json),
            "revoked_at": e.revoked_at,
            "revoked_by": e.revoked_by,
            "revoked_actor_type": e.revoked_actor_type,
            "revoked_reason": e.revoked_reason,
            "created": created,
        }
    )


class GraphService:
    def _reachable(self, uow: UnitOfWork, start: str, goal: str) -> bool:
        """BFS over the *effective* DAG: active structural edges only."""
        seen = {start}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if node == goal:
                return True
            for nxt in uow.edges.active_successors(node):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return False

    def add_edge(self, uow: UnitOfWork, cmd: EdgeCreate, actor: Actor) -> EdgeRead:
        if cmd.from_memory_id == cmd.to_memory_id:
            raise CycleDetectedError("Self-loop is not allowed in Structural DAG")
        for mid in (cmd.from_memory_id, cmd.to_memory_id):
            m = uow.memories.get(mid)
            if m is None:
                raise NotFoundError("Memory not found", {"memory_id": mid})
            if m.status in FROZEN_LINK_STATUSES:  # Q-N11 (DB trigger is the backstop)
                raise LinkEndpointFrozenError(
                    "archived/rejected memories cannot get new edges",
                    {"memory_id": mid, "status": m.status},
                )
        existing = uow.edges.find_active(cmd.from_memory_id, cmd.to_memory_id, cmd.edge_type.value)
        if existing is not None:
            return edge_read(existing, created=False)
        if cmd.edge_type is EdgeType.PARENT:
            parents = uow.edges.active_incoming(cmd.to_memory_id, EdgeType.PARENT.value)
            if parents:
                raise ParentAlreadyExistsError(
                    "Memory already has an active PARENT edge; revoke it before re-parenting "
                    "or use a REFERENCE edge",
                    {
                        "to_memory_id": cmd.to_memory_id,
                        "existing_parent_id": parents[0].from_memory_id,
                        "existing_edge_id": parents[0].edge_id,
                    },
                )
        if self._reachable(uow, cmd.to_memory_id, cmd.from_memory_id):
            raise CycleDetectedError(
                "Edge would create a cycle in Structural DAG",
                {"from_memory_id": cmd.from_memory_id, "to_memory_id": cmd.to_memory_id},
            )
        edge = MemoryEdge(
            edge_id=new_id("edg"),
            from_memory_id=cmd.from_memory_id,
            to_memory_id=cmd.to_memory_id,
            edge_type=cmd.edge_type.value,
            status="active",
            created_by=actor.actor_id,
            created_at=utc_now(),
            metadata_json=json.dumps(cmd.metadata, ensure_ascii=False),
        )
        uow.edges.add(edge)
        audit.record(
            uow, AuditEventType.EDGE_CREATED, AuditEntityType.EDGE, edge.edge_id, actor,
            "edge created", memory_id=edge.from_memory_id, related_memory_id=edge.to_memory_id,
            after=audit.edge_state(edge),
        )  # fmt: skip
        return edge_read(edge, created=True)

    def revoke_edge(
        self,
        uow: UnitOfWork,
        edge: MemoryEdge,
        actor: Actor,
        reason: str,
        *,
        via_state_machine: bool = False,
    ) -> MemoryEdge:
        """Mark an edge revoked; the row is kept forever (history)."""
        if not reason.strip():
            raise ValidationFailedError("reason is required")
        if edge.status != "active":
            raise InvalidTransitionError("Edge is already revoked", {"edge_id": edge.edge_id})
        if edge.edge_type == EdgeType.SUPERSEDES.value and not via_state_machine:
            raise ValidationFailedError(
                "SUPERSEDES edges are revoked only via status transition T11",
                {"edge_id": edge.edge_id},
            )
        if actor.actor_type not in REVOKERS and not via_state_machine:
            raise TransitionNotPermittedError("Actor may not revoke edges")
        before = audit.edge_state(edge)
        uow.edges.revoke(edge, actor.actor_type.value, actor.actor_id, reason, utc_now())
        audit.record(
            uow, AuditEventType.EDGE_REVOKED, AuditEntityType.EDGE, edge.edge_id, actor, reason,
            memory_id=edge.from_memory_id, related_memory_id=edge.to_memory_id,
            before=before, after=audit.edge_state(edge),
        )  # fmt: skip
        return edge

"""RelationService: Semantic Relations (ADR-007/008). Never performs DAG cycle detection."""

from __future__ import annotations

import json

from ame.application import audit
from ame.application.ids import new_id, utc_now
from ame.application.schemas import Actor, RelationCreate, RelationRead
from ame.domain.enums import (
    DIRECTED_RELATION_TYPES,
    FROZEN_LINK_STATUSES,
    ActorType,
    AuditEntityType,
    AuditEventType,
)
from ame.errors import (
    InvalidTransitionError,
    LinkEndpointFrozenError,
    NotFoundError,
    TransitionNotPermittedError,
    ValidationFailedError,
)
from ame.infrastructure.models import MemoryRelation
from ame.infrastructure.unit_of_work import UnitOfWork

REVOKERS = frozenset({ActorType.USER, ActorType.AGENT})


def relation_read(
    r: MemoryRelation, created: bool = False, notice: str | None = None
) -> RelationRead:
    return RelationRead.model_validate(
        {
            "relation_id": r.relation_id,
            "relation_type": r.relation_type,
            "directed": r.relation_type in DIRECTED_RELATION_TYPES,
            "from_memory_id": r.from_memory_id,
            "to_memory_id": r.to_memory_id,
            "status": r.status,
            "created_by": r.created_by,
            "created_at": r.created_at,
            "metadata": json.loads(r.metadata_json),
            "revoked_at": r.revoked_at,
            "revoked_by": r.revoked_by,
            "revoked_actor_type": r.revoked_actor_type,
            "revoked_reason": r.revoked_reason,
            "created": created,
            "review_notice": notice,
        }
    )


class RelationService:
    @staticmethod
    def endpoints(cmd: RelationCreate) -> tuple[str, str]:
        if cmd.relation_type in DIRECTED_RELATION_TYPES:
            return cmd.from_memory_id, cmd.to_memory_id
        low, high = sorted((cmd.from_memory_id, cmd.to_memory_id))
        return low, high

    def add_relation(
        self, uow: UnitOfWork, cmd: RelationCreate, actor: Actor
    ) -> tuple[MemoryRelation, bool]:
        if cmd.from_memory_id == cmd.to_memory_id:
            raise ValidationFailedError("A memory cannot relate to itself")
        for mid in (cmd.from_memory_id, cmd.to_memory_id):
            m = uow.memories.get(mid)
            if m is None:
                raise NotFoundError("Memory not found", {"memory_id": mid})
            if m.status in FROZEN_LINK_STATUSES:  # Q-N11
                raise LinkEndpointFrozenError(
                    "archived/rejected memories cannot get new relations",
                    {"memory_id": mid, "status": m.status},
                )
        frm, to = self.endpoints(cmd)
        existing = uow.relations.find_active(cmd.relation_type.value, frm, to)
        if existing is not None:
            return existing, False
        rel = MemoryRelation(
            relation_id=new_id("rel"),
            relation_type=cmd.relation_type.value,
            from_memory_id=frm,
            to_memory_id=to,
            status="active",
            created_by=actor.actor_id,
            created_at=utc_now(),
            metadata_json=json.dumps(cmd.metadata, ensure_ascii=False),
        )
        uow.relations.add(rel)
        audit.record(
            uow, AuditEventType.RELATION_CREATED, AuditEntityType.RELATION, rel.relation_id,
            actor, "relation created", memory_id=frm, related_memory_id=to,
            after=audit.relation_state(rel),
        )  # fmt: skip
        return rel, True

    def revoke_relation(
        self, uow: UnitOfWork, relation_id: str, actor: Actor, reason: str
    ) -> MemoryRelation:
        rel = uow.relations.get(relation_id)
        if rel is None:
            raise NotFoundError("Relation not found", {"relation_id": relation_id})
        if rel.status != "active":
            raise InvalidTransitionError("Relation is already revoked")
        if actor.actor_type not in REVOKERS:
            raise TransitionNotPermittedError("Actor may not revoke relations")
        if not reason.strip():
            raise ValidationFailedError("reason is required")
        before = audit.relation_state(rel)
        uow.relations.revoke(rel, actor.actor_type.value, actor.actor_id, reason, utc_now())
        audit.record(
            uow, AuditEventType.RELATION_REVOKED, AuditEntityType.RELATION, rel.relation_id,
            actor, reason, memory_id=rel.from_memory_id, related_memory_id=rel.to_memory_id,
            before=before, after=audit.relation_state(rel),
        )  # fmt: skip
        return rel

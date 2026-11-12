"""Audit helper: every business operation records an append-only event in the SAME
transaction as the change (ADR-013). before/after carry state + content_hash only,
never memory content (Q-D2, AGENTS rule: no full memory content in logs/audit)."""

from __future__ import annotations

import json
from typing import Any

from ame.application.ids import new_id, utc_now
from ame.application.schemas import Actor
from ame.domain.enums import AuditEntityType, AuditEventType
from ame.infrastructure.models import Memory, MemoryEdge, MemoryRelation
from ame.infrastructure.unit_of_work import UnitOfWork

_FORBIDDEN_KEYS = frozenset({"content", "summary", "excerpt"})


def _dump(d: dict[str, Any] | None) -> str | None:
    if d is None:
        return None
    bad = _FORBIDDEN_KEYS & set(d)
    if bad:  # defensive: a programming error, never let content into audit
        raise ValueError(f"audit payload must not contain {sorted(bad)}")
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def record(
    uow: UnitOfWork,
    event_type: AuditEventType,
    entity_type: AuditEntityType,
    entity_id: str,
    actor: Actor,
    reason: str,
    *,
    memory_id: str | None = None,
    related_memory_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    from ame.infrastructure.models import AuditEvent

    uow.audit.add(
        AuditEvent(
            event_id=new_id("evt"),
            seq=uow.audit.next_seq(),
            event_type=event_type.value,
            entity_type=entity_type.value,
            entity_id=entity_id,
            memory_id=memory_id,
            related_memory_id=related_memory_id,
            actor_type=actor.actor_type.value,
            actor_id=actor.actor_id,
            occurred_at=utc_now(),
            reason=reason,
            before_json=_dump(before),
            after_json=_dump(after),
            correlation_id=uow.correlation_id,
        )
    )


def memory_state(uow: UnitOfWork, m: Memory) -> dict[str, Any]:
    v = uow.versions.get(m.memory_id, m.current_version)
    return {
        "status": m.status,
        "layer": m.layer,
        "memory_type": m.memory_type,
        "current_version": m.current_version,
        "content_hash": v.content_hash if v else None,
    }


def edge_state(e: MemoryEdge) -> dict[str, Any]:
    return {
        "edge_type": e.edge_type,
        "from_memory_id": e.from_memory_id,
        "to_memory_id": e.to_memory_id,
        "status": e.status,
    }


def relation_state(r: MemoryRelation) -> dict[str, Any]:
    return {
        "relation_type": r.relation_type,
        "from_memory_id": r.from_memory_id,
        "to_memory_id": r.to_memory_id,
        "status": r.status,
    }

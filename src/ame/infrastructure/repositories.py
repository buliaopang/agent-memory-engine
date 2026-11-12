"""Repositories: the only place that issues SQL (AGENTS.md rule 6)."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select, update
from sqlalchemy.orm import Session

from ame.infrastructure.models import (
    Agent,
    AuditEvent,
    IdempotencyKey,
    Memory,
    MemoryAuditEvent,
    MemoryConflict,
    MemoryEdge,
    MemoryRelation,
    MemorySource,
    MemoryTag,
    MemoryVersion,
    Space,
    SpaceMember,
)


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, memory: Memory) -> None:
        self._s.add(memory)
        self._s.flush()

    def get(self, memory_id: str) -> Memory | None:
        return self._s.get(Memory, memory_id)

    def exists(self, memory_id: str) -> bool:
        return self.get(memory_id) is not None

    def bump_current_version(self, memory_id: str, expected_version: int, updated_at: str) -> bool:
        """Optimistic lock: only succeeds if current_version == expected_version."""
        result = cast(
            CursorResult[Any],
            self._s.execute(
                update(Memory)
                .where(Memory.memory_id == memory_id, Memory.current_version == expected_version)
                .values(current_version=expected_version + 1, updated_at=updated_at)
                .execution_options(synchronize_session=False)
            ),
        )
        ok = result.rowcount == 1
        memory = self._s.get(Memory, memory_id)
        if memory is not None:
            self._s.refresh(memory)
        return ok

    def flush(self) -> None:
        self._s.flush()


class VersionRepository:
    """Append-only: no update/delete methods by design."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, version: MemoryVersion) -> None:
        self._s.add(version)
        self._s.flush()

    def get(self, memory_id: str, version: int) -> MemoryVersion | None:
        return self._s.scalars(
            select(MemoryVersion).where(
                MemoryVersion.memory_id == memory_id, MemoryVersion.version == version
            )
        ).first()

    def list_for(self, memory_id: str) -> list[MemoryVersion]:
        return list(
            self._s.scalars(
                select(MemoryVersion)
                .where(MemoryVersion.memory_id == memory_id)
                .order_by(MemoryVersion.version)
            )
        )


def _revoke(link: MemoryEdge | MemoryRelation, actor_type: str, actor_id: str, reason: str,
            now: str) -> None:  # fmt: skip
    link.status = "revoked"
    link.revoked_at = now
    link.revoked_by = actor_id
    link.revoked_actor_type = actor_type
    link.revoked_reason = reason


class EdgeRepository:
    """Structural DAG edges only. Active edges form the effective DAG (ADR-006)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, edge: MemoryEdge) -> None:
        self._s.add(edge)
        self._s.flush()

    def get(self, edge_id: str) -> MemoryEdge | None:
        return self._s.get(MemoryEdge, edge_id)

    def find_active(
        self, from_memory_id: str, to_memory_id: str, edge_type: str
    ) -> MemoryEdge | None:
        return self._s.scalars(
            select(MemoryEdge).where(
                MemoryEdge.from_memory_id == from_memory_id,
                MemoryEdge.to_memory_id == to_memory_id,
                MemoryEdge.edge_type == edge_type,
                MemoryEdge.status == "active",
            )
        ).first()

    def active_successors(self, memory_id: str) -> list[str]:
        """Active structural successors regardless of edge_type; revoked edges are ignored."""
        return list(
            self._s.scalars(
                select(MemoryEdge.to_memory_id).where(
                    MemoryEdge.from_memory_id == memory_id, MemoryEdge.status == "active"
                )
            )
        )

    def _active(self, col: Any, memory_id: str, edge_type: str) -> list[MemoryEdge]:
        return list(
            self._s.scalars(
                select(MemoryEdge)
                .where(
                    col == memory_id,
                    MemoryEdge.edge_type == edge_type,
                    MemoryEdge.status == "active",
                )
                .order_by(MemoryEdge.created_at, MemoryEdge.edge_id)
            )
        )

    def active_incoming(self, memory_id: str, edge_type: str) -> list[MemoryEdge]:
        return self._active(MemoryEdge.to_memory_id, memory_id, edge_type)

    def active_outgoing(self, memory_id: str, edge_type: str) -> list[MemoryEdge]:
        return self._active(MemoryEdge.from_memory_id, memory_id, edge_type)

    def list_for(self, memory_id: str, include_revoked: bool) -> list[MemoryEdge]:
        stmt = select(MemoryEdge).where(
            or_(MemoryEdge.from_memory_id == memory_id, MemoryEdge.to_memory_id == memory_id)
        )
        if not include_revoked:
            stmt = stmt.where(MemoryEdge.status == "active")
        return list(self._s.scalars(stmt.order_by(MemoryEdge.created_at, MemoryEdge.edge_id)))

    def revoke(
        self, edge: MemoryEdge, actor_type: str, actor_id: str, reason: str, now: str
    ) -> None:
        _revoke(edge, actor_type, actor_id, reason, now)
        self._s.flush()


class RelationRepository:
    """Semantic relations only (separate table, repository and validation; ADR-007)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, relation: MemoryRelation) -> None:
        self._s.add(relation)
        self._s.flush()

    def get(self, relation_id: str) -> MemoryRelation | None:
        return self._s.get(MemoryRelation, relation_id)

    def find_active(
        self, relation_type: str, from_memory_id: str, to_memory_id: str
    ) -> MemoryRelation | None:
        return self._s.scalars(
            select(MemoryRelation).where(
                MemoryRelation.relation_type == relation_type,
                MemoryRelation.from_memory_id == from_memory_id,
                MemoryRelation.to_memory_id == to_memory_id,
                MemoryRelation.status == "active",
            )
        ).first()

    def list_for(self, memory_id: str, include_revoked: bool) -> list[MemoryRelation]:
        stmt = select(MemoryRelation).where(
            or_(
                MemoryRelation.from_memory_id == memory_id,
                MemoryRelation.to_memory_id == memory_id,
            )
        )
        if not include_revoked:
            stmt = stmt.where(MemoryRelation.status == "active")
        return list(
            self._s.scalars(stmt.order_by(MemoryRelation.created_at, MemoryRelation.relation_id))
        )

    def revoke(
        self, relation: MemoryRelation, actor_type: str, actor_id: str, reason: str, now: str
    ) -> None:
        _revoke(relation, actor_type, actor_id, reason, now)
        self._s.flush()


class TagRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add_many(self, memory_id: str, tags: list[str]) -> None:
        self._s.add_all([MemoryTag(memory_id=memory_id, tag=t) for t in tags])
        self._s.flush()

    def list_for(self, memory_id: str) -> list[str]:
        return list(
            self._s.scalars(
                select(MemoryTag.tag)
                .where(MemoryTag.memory_id == memory_id)
                .order_by(MemoryTag.tag)
            )
        )

    def remove_many(self, memory_id: str, tags: list[str]) -> None:
        if tags:
            self._s.execute(
                delete(MemoryTag).where(MemoryTag.memory_id == memory_id, MemoryTag.tag.in_(tags))
            )
            self._s.flush()

    def memory_ids_with(self, tag: str) -> list[str]:
        return list(
            self._s.scalars(
                select(MemoryTag.memory_id)
                .where(MemoryTag.tag == tag)
                .order_by(MemoryTag.memory_id)
            )
        )


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, source: MemorySource) -> None:
        self._s.add(source)
        self._s.flush()

    def list_for(self, memory_id: str) -> list[MemorySource]:
        return list(
            self._s.scalars(
                select(MemorySource)
                .where(MemorySource.memory_id == memory_id)
                .order_by(MemorySource.created_at, MemorySource.source_id)
            )
        )


class LegacyAuditRepository:
    """Read-only access to the frozen 0002 memory_audit_events table (ADR-013)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def list_for(self, memory_id: str) -> list[MemoryAuditEvent]:
        return list(
            self._s.scalars(
                select(MemoryAuditEvent)
                .where(MemoryAuditEvent.memory_id == memory_id)
                .order_by(MemoryAuditEvent.created_at, MemoryAuditEvent.audit_id)
            )
        )


class AuditRepository:
    """Unified append-only audit_events (ADR-013). No update/delete methods by design.

    seq is allocated inside the write transaction (BEGIN IMMEDIATE holds the write lock),
    so it is gap-free and totally ordered across concurrent agents; UNIQUE(seq) backs it.
    """

    def __init__(self, session: Session) -> None:
        self._s = session

    def next_seq(self) -> int:
        return int(self._s.scalar(select(func.coalesce(func.max(AuditEvent.seq), 0))) or 0) + 1

    def add(self, event: AuditEvent) -> None:
        self._s.add(event)
        self._s.flush()

    def list_for(self, memory_id: str) -> list[AuditEvent]:
        return list(
            self._s.scalars(
                select(AuditEvent)
                .where(
                    or_(
                        AuditEvent.memory_id == memory_id, AuditEvent.related_memory_id == memory_id
                    )
                )
                .order_by(AuditEvent.seq)
            )
        )

    def list_by_correlation(self, correlation_id: str) -> list[AuditEvent]:
        return list(
            self._s.scalars(
                select(AuditEvent)
                .where(AuditEvent.correlation_id == correlation_id)
                .order_by(AuditEvent.seq)
            )
        )

    def pre_archive_status(self, memory_id: str) -> str | None:
        """Status the memory had right before its most recent archive (T10 restore target)."""
        ev = self._s.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.memory_id == memory_id,
                AuditEvent.entity_type == "memory",
                AuditEvent.event_type.in_(("MEMORY_STATUS_CHANGED", "MEMORY_RESTORED")),
                func.json_extract(AuditEvent.after_json, "$.status") == "archived",
            )
            .order_by(AuditEvent.seq.desc())
        ).first()
        if ev is None or ev.before_json is None:
            return None
        return cast(
            str | None, self._s.scalar(select(func.json_extract(ev.before_json, "$.status")))
        )


class ConflictRepository:
    """Substantive conflicts (ADR-012). Rows are never deleted; open -> resolved only."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, conflict: MemoryConflict) -> None:
        self._s.add(conflict)
        self._s.flush()

    def get(self, conflict_id: str) -> MemoryConflict | None:
        return self._s.get(MemoryConflict, conflict_id)

    def open_for_relation(self, relation_id: str) -> MemoryConflict | None:
        return self._s.scalars(
            select(MemoryConflict).where(
                MemoryConflict.relation_id == relation_id, MemoryConflict.status == "open"
            )
        ).first()

    def list_for(self, memory_id: str, include_resolved: bool) -> list[MemoryConflict]:
        stmt = select(MemoryConflict).where(
            or_(MemoryConflict.memory_a_id == memory_id, MemoryConflict.memory_b_id == memory_id)
        )
        if not include_resolved:
            stmt = stmt.where(MemoryConflict.status == "open")
        return list(
            self._s.scalars(stmt.order_by(MemoryConflict.opened_at, MemoryConflict.conflict_id))
        )

    def count_open(self, memory_id: str) -> int:
        return len(self.list_for(memory_id, include_resolved=False))

    def resolve(
        self,
        c: MemoryConflict,
        *,
        actor_type: str,
        actor_id: str,
        resolution: str,
        reason: str,
        evidence_json: str,
        now: str,
    ) -> None:
        c.status = "resolved"
        c.resolved_at = now
        c.resolved_by = actor_id
        c.resolved_actor_type = actor_type
        c.resolution = resolution
        c.resolution_reason = reason
        c.resolution_evidence_json = evidence_json
        self._s.flush()


class SpaceRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, space: Space) -> None:
        self._s.add(space)
        self._s.flush()

    def get(self, space_id: str) -> Space | None:
        return self._s.get(Space, space_id)

    def list_all(self) -> list[Space]:
        return list(self._s.scalars(select(Space).order_by(Space.space_id)))

    def shared_ids(self) -> set[str]:
        return set(self._s.scalars(select(Space.space_id).where(Space.scope_type == "shared")))

    # ---- Phase 3a: space_members (ADR-025); rows are never deleted (trigger) ----
    def member(self, space_id: str, agent_id: str) -> SpaceMember | None:
        return self._s.get(SpaceMember, (space_id, agent_id))

    def add_member(self, m: SpaceMember) -> None:
        self._s.add(m)
        self._s.flush()

    def active_grants(self, agent_id: str) -> list[tuple[str, str]]:
        rows = self._s.execute(
            select(SpaceMember.space_id, SpaceMember.role).where(
                SpaceMember.agent_id == agent_id, SpaceMember.revoked_at.is_(None)
            )
        ).all()
        return [(r[0], r[1]) for r in rows]

    def members_of(self, space_id: str) -> list[SpaceMember]:
        return list(
            self._s.scalars(
                select(SpaceMember)
                .where(SpaceMember.space_id == space_id)
                .order_by(SpaceMember.agent_id)
            )
        )


class AgentRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, agent: Agent) -> None:
        self._s.add(agent)
        self._s.flush()

    def get(self, agent_id: str) -> Agent | None:
        return self._s.get(Agent, agent_id)

    def list_all(self) -> list[Agent]:
        return list(self._s.scalars(select(Agent).order_by(Agent.agent_id)))

    def by_credential_hash(self, digest: str) -> Agent | None:
        return self._s.scalars(select(Agent).where(Agent.credential_hash == digest)).first()


class IdempotencyRepository:
    """Replay cache rows (ADR-027). Callers hold a BEGIN IMMEDIATE write transaction."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, actor_id: str, idem_key: str) -> IdempotencyKey | None:
        return self._s.get(IdempotencyKey, (actor_id, idem_key))

    def add(self, row: IdempotencyKey) -> None:
        self._s.add(row)
        self._s.flush()

    def remove(self, row: IdempotencyKey) -> None:
        self._s.delete(row)
        self._s.flush()

    def purge_expired(self, now: str, limit: int | None = None) -> int:
        ids = select(IdempotencyKey.actor_id, IdempotencyKey.idem_key).where(
            IdempotencyKey.expires_at <= now
        )
        if limit is not None:
            ids = ids.limit(limit)
        keys = list(self._s.execute(ids).all())
        for a, k in keys:
            self._s.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.actor_id == a, IdempotencyKey.idem_key == k
                )
            )
        return len(keys)

    def count_by_state(self) -> dict[str, int]:
        rows = self._s.execute(
            select(IdempotencyKey.state, func.count()).group_by(IdempotencyKey.state)
        ).all()
        return {str(s): int(n) for s, n in rows}

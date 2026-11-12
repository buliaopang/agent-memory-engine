"""Memory application service. REST and MCP adapters must both call this (ADR/AGENTS rule 7)."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session, sessionmaker

from ame.application import audit
from ame.application.access import UNRESTRICTED, Access, resolve
from ame.application.conflict_service import (
    ConflictService,
    conflict_read,
    suggest_for_conflicts,
)
from ame.application.graph_service import GraphService, edge_read
from ame.application.ids import new_id, sha256, utc_now
from ame.application.lifecycle import LifecycleService
from ame.application.relation_service import RelationService, relation_read
from ame.application.schemas import (
    Actor,
    AuditEventRead,
    ClassificationChange,
    ConflictRead,
    ConflictResolve,
    EdgeCreate,
    EdgeRead,
    LinkRevoke,
    MemoryCreate,
    MemoryPatch,
    MemoryRead,
    RelationCreate,
    RelationRead,
    SourceRead,
    StatusChange,
    VersionCreate,
    VersionRead,
)
from ame.domain.enums import (
    ActorType,
    AuditEntityType,
    AuditEventType,
    EdgeType,
    MemoryStatus,
    RelationType,
)
from ame.domain.state_machine import initial_status
from ame.errors import (
    InvalidTransitionError,
    MemoryArchivedError,
    NotFoundError,
    ReasonRequiredError,
    SpaceNotFoundError,
    ValidationFailedError,
    VersionConflictError,
)
from ame.infrastructure.models import Memory, MemorySource, MemoryVersion
from ame.infrastructure.unit_of_work import UnitOfWork

SYSTEM_ACTOR = Actor(actor_type=ActorType.SYSTEM, actor_id="system")


class MemoryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        auto_approve_agent_writes: bool = False,
    ) -> None:
        self._sf = session_factory
        self._auto_approve = auto_approve_agent_writes
        self._graph = GraphService()
        self._relations = RelationService()
        self._life = LifecycleService(self._graph)
        self._conflicts = ConflictService(self._life, self._relations)

    # ---------- helpers ----------
    def _uow(self, read_only: bool = False) -> UnitOfWork:
        """Write use cases: BEGIN IMMEDIATE; reads: DEFERRED (ADR-010)."""
        return UnitOfWork(self._sf, read_only=read_only)

    @staticmethod
    def _require(uow: UnitOfWork, memory_id: str) -> Memory:
        m = uow.memories.get(memory_id)
        if m is None:
            raise NotFoundError("Memory not found", {"memory_id": memory_id})
        return m

    @staticmethod
    def _see(uow: UnitOfWork, acc: Access, memory_id: str) -> Memory:
        """404 for missing AND for invisible memories (decision 5: no existence leak)."""
        return acc.see_memory(uow.memories.get(memory_id), memory_id)

    @staticmethod
    def _visible_ids(uow: UnitOfWork, acc: Access, ids: list[str]) -> list[str]:
        if acc.readable is None:
            return ids
        out = []
        for i in ids:
            m = uow.memories.get(i)
            if m is not None and acc.can_read(m.scope_id):
                out.append(i)
        return out

    def _guard_link(self, uow: UnitOfWork, acc: Access, from_id: str, to_id: str) -> None:
        """Agent links: both endpoints visible, write access to the from-memory's space."""
        if not acc.is_agent:
            return
        src = self._see(uow, acc, from_id)
        self._see(uow, acc, to_id)
        acc.require_write_space(src.scope_id)

    @staticmethod
    def _read(uow: UnitOfWork, m: Memory, acc: Access = UNRESTRICTED) -> MemoryRead:
        v = uow.versions.get(m.memory_id, m.current_version)
        assert v is not None, "current_version must exist"
        sources = [
            SourceRead.model_validate(
                {
                    "source_id": s.source_id,
                    "memory_id": s.memory_id,
                    "source_type": s.source_type,
                    "source_ref": s.source_ref,
                    "excerpt": s.excerpt,
                    "content_hash": s.content_hash,
                    "created_at": s.created_at,
                    "metadata": json.loads(s.metadata_json),
                }
            )
            for s in uow.sources.list_for(m.memory_id)
        ]
        return MemoryRead.model_validate(
            {
                "memory_id": m.memory_id,
                "current_version": m.current_version,
                "version_id": v.version_id,
                "content": v.content,
                "summary": v.summary,
                "content_hash": v.content_hash,
                "memory_type": m.memory_type,
                "layer": m.layer,
                "scope_type": m.scope_type,
                "scope_id": m.scope_id,
                "created_by": m.created_by,
                "created_at": m.created_at,
                "updated_at": m.updated_at,
                "importance": m.importance,
                "confidence": m.confidence,
                "status": m.status,
                "tags": uow.tags.list_for(m.memory_id),
                "sources": sources,
                "parent_ids": MemoryService._visible_ids(
                    uow,
                    acc,
                    [
                        e.from_memory_id
                        for e in uow.edges.active_incoming(m.memory_id, EdgeType.PARENT.value)
                    ],
                ),
                "reference_ids": MemoryService._visible_ids(
                    uow,
                    acc,
                    [
                        e.to_memory_id
                        for e in uow.edges.active_outgoing(m.memory_id, EdgeType.REFERENCE.value)
                    ],
                ),
                "metadata": json.loads(m.metadata_json),
                "vector_status": uow.outbox.vector_status(m.memory_id, m.current_version),
            }
        )

    # ---------- use cases ----------
    def create_memory(self, cmd: MemoryCreate, actor: Actor) -> MemoryRead:
        with self._uow() as uow:
            acc = resolve(uow, actor)
            mid = self.create_in(uow, acc, cmd, actor)
            result = self._read(uow, self._require(uow, mid), acc)
            uow.commit()
            return result

    def create_in(self, uow: UnitOfWork, acc: Access, cmd: MemoryCreate, actor: Actor) -> str:
        """Create inside the caller's transaction (also used by distill, Phase 4e)."""
        space = uow.spaces.get(cmd.scope_id)
        if (
            space is None
            or space.scope_type != cmd.scope_type.value
            or not acc.can_read(space.space_id)
        ):
            raise SpaceNotFoundError(
                "No space with this scope_id and scope_type",
                {"scope_id": cmd.scope_id, "scope_type": cmd.scope_type.value},
            )
        acc.require_write_space(space.space_id)
        for mid in [*cmd.parent_ids, *cmd.reference_ids]:
            self._see(uow, acc, mid)
        status, trigger = initial_status(actor.actor_type)
        if self._auto_approve and status is MemoryStatus.CANDIDATE:
            status = MemoryStatus.ACTIVE
        now = utc_now()
        mid = new_id("mem")
        uow.memories.add(
            Memory(
                memory_id=mid,
                memory_type=cmd.memory_type.value,
                layer=cmd.layer.value,
                scope_type=cmd.scope_type.value,
                scope_id=cmd.scope_id,
                created_by=actor.actor_id,
                created_at=now,
                updated_at=now,
                importance=cmd.importance,
                confidence=cmd.confidence,
                status=status.value,
                current_version=1,
                metadata_json=json.dumps(cmd.metadata, ensure_ascii=False),
            )
        )
        uow.versions.add(
            MemoryVersion(
                version_id=new_id("ver"),
                memory_id=mid,
                version=1,
                content=cmd.content,
                summary=cmd.summary,
                content_hash=sha256(cmd.content),
                created_by=actor.actor_id,
                created_at=now,
                change_reason=None,
            )
        )
        uow.tags.add_many(mid, cmd.tags)
        for s in cmd.sources:
            uow.sources.add(
                MemorySource(
                    source_id=new_id("src"),
                    memory_id=mid,
                    source_type=s.source_type.value,
                    source_ref=s.source_ref,
                    excerpt=s.excerpt,
                    content_hash=s.content_hash,
                    created_at=now,
                    metadata_json=json.dumps(s.metadata, ensure_ascii=False),
                )
            )
        for pid in cmd.parent_ids:
            edge = EdgeCreate(from_memory_id=pid, to_memory_id=mid, edge_type=EdgeType.PARENT)
            self._graph.add_edge(uow, edge, actor)
        for rid in cmd.reference_ids:
            ref = EdgeType.REFERENCE
            edge = EdgeCreate(from_memory_id=mid, to_memory_id=rid, edge_type=ref)
            self._graph.add_edge(uow, edge, actor)
        created = self._require(uow, mid)
        audit.record(
            uow, AuditEventType.MEMORY_CREATED, AuditEntityType.MEMORY, mid, actor, "created",
            memory_id=mid, after={**audit.memory_state(uow, created), "trigger": trigger},
        )  # fmt: skip
        return mid

    def get_memory(self, memory_id: str, actor: Actor | None = None) -> MemoryRead:
        with self._uow(read_only=True) as uow:
            acc = resolve(uow, actor)
            return self._read(uow, self._see(uow, acc, memory_id), acc)

    def create_version(self, memory_id: str, cmd: VersionCreate, actor: Actor) -> VersionRead:
        with self._uow() as uow:
            acc = resolve(uow, actor)
            m = self._see(uow, acc, memory_id)
            acc.require_edit(m, allow_active=self._auto_approve)
            if m.status == MemoryStatus.ARCHIVED.value:
                raise MemoryArchivedError(
                    "Memory is archived; restore it before creating a version",
                    {"memory_id": memory_id},
                )
            if m.current_version != cmd.expected_version or not uow.memories.bump_current_version(
                memory_id, cmd.expected_version, utc_now()
            ):
                raise VersionConflictError(
                    "Expected version does not match current version",
                    {"current_version": m.current_version},
                )
            new_version = cmd.expected_version + 1
            v = MemoryVersion(
                version_id=new_id("ver"),
                memory_id=memory_id,
                version=new_version,
                content=cmd.content,
                summary=cmd.summary,
                content_hash=sha256(cmd.content),
                created_by=actor.actor_id,
                created_at=utc_now(),
                change_reason=cmd.change_reason,
            )
            uow.versions.add(v)
            old = uow.versions.get(memory_id, cmd.expected_version)
            audit.record(
                uow, AuditEventType.MEMORY_VERSION_CREATED, AuditEntityType.VERSION, v.version_id,
                actor, cmd.change_reason, memory_id=memory_id,
                before={"version": cmd.expected_version,
                        "content_hash": old.content_hash if old else None},
                after={"version": new_version, "content_hash": v.content_hash},
            )  # fmt: skip
            uow.commit()
            return self._version_read(v)

    @staticmethod
    def _version_read(v: MemoryVersion) -> VersionRead:
        return VersionRead.model_validate({k: getattr(v, k) for k in VersionRead.model_fields})

    def list_versions(self, memory_id: str, actor: Actor | None = None) -> list[VersionRead]:
        with self._uow(read_only=True) as uow:
            self._see(uow, resolve(uow, actor), memory_id)
            return [self._version_read(v) for v in uow.versions.list_for(memory_id)]

    def change_status(self, memory_id: str, cmd: StatusChange, actor: Actor) -> MemoryRead:
        with self._uow() as uow:
            acc = resolve(uow, actor)
            m = self._see(uow, acc, memory_id)
            acc.require_edit(m, allow_active=self._auto_approve)
            if cmd.superseded_by_memory_id is not None:
                self._see(uow, acc, cmd.superseded_by_memory_id)
            self._life.set_status(
                uow, m, cmd.to_status, actor, cmd.reason, cmd.superseded_by_memory_id
            )
            result = self._read(uow, m, acc)
            uow.commit()
            return result

    def change_classification(
        self, memory_id: str, cmd: ClassificationChange, actor: Actor
    ) -> MemoryRead:
        """ADR-004: layer may change (e.g. promotion); memory_type needs an explicit reason."""
        with self._uow() as uow:
            acc = resolve(uow, actor)
            m = self._see(uow, acc, memory_id)
            acc.require_edit(m, allow_active=self._auto_approve)
            changes: dict[str, tuple[str, str]] = {}
            if cmd.layer is not None and cmd.layer.value != m.layer:
                changes["layer"] = (m.layer, cmd.layer.value)
            if cmd.memory_type is not None and cmd.memory_type.value != m.memory_type:
                if not (cmd.reason and cmd.reason.strip()):
                    raise ReasonRequiredError("Changing memory_type requires a reason")
                changes["memory_type"] = (m.memory_type, cmd.memory_type.value)
            if not changes:
                raise ValidationFailedError("No classification change requested")
            for field, (_old, new) in changes.items():
                setattr(m, field, new)
            m.updated_at = utc_now()
            uow.memories.flush()
            audit.record(
                uow, AuditEventType.MEMORY_UPDATED, AuditEntityType.MEMORY, memory_id, actor,
                (cmd.reason or "layer change").strip(), memory_id=memory_id,
                before={k: v[0] for k, v in changes.items()},
                after={k: v[1] for k, v in changes.items()},
            )  # fmt: skip
            result = self._read(uow, m, acc)
            uow.commit()
            return result

    def patch_memory(self, memory_id: str, cmd: MemoryPatch, actor: Actor) -> MemoryRead:
        """Mutable metadata only (importance, tags); audited as MEMORY_UPDATED (ADR-017)."""
        with self._uow() as uow:
            acc = resolve(uow, actor)
            m = self._see(uow, acc, memory_id)
            acc.require_edit(m, allow_active=self._auto_approve)
            if m.status == MemoryStatus.ARCHIVED.value:
                raise MemoryArchivedError(
                    "Memory is archived; restore it before changing it", {"memory_id": memory_id}
                )
            before: dict[str, object] = {}
            after: dict[str, object] = {}
            if cmd.importance is not None and cmd.importance != m.importance:
                before["importance"], after["importance"] = m.importance, cmd.importance
                m.importance = cmd.importance
            if cmd.tags is not None:
                old = uow.tags.list_for(memory_id)
                new = sorted(cmd.tags)
                if old != new:
                    uow.tags.remove_many(memory_id, [t for t in old if t not in new])
                    uow.tags.add_many(memory_id, [t for t in new if t not in old])
                    before["tags"], after["tags"] = old, new
            if not after:
                raise ValidationFailedError("No metadata change requested")
            m.updated_at = utc_now()
            uow.memories.flush()
            audit.record(
                uow, AuditEventType.MEMORY_UPDATED, AuditEntityType.MEMORY, memory_id, actor,
                cmd.reason, memory_id=memory_id, before=before, after=after,
            )  # fmt: skip
            result = self._read(uow, m, acc)
            uow.commit()
            return result

    def archive_memory(self, memory_id: str, reason: str, actor: Actor) -> MemoryRead:
        """T9 wrapper (no physical delete)."""
        return self.change_status(
            memory_id, StatusChange(to_status=MemoryStatus.ARCHIVED, reason=reason), actor
        )

    def restore_memory(self, memory_id: str, reason: str, actor: Actor) -> MemoryRead:
        """T10 wrapper: returns to the pre-archive status recorded in the audit log."""
        with self._uow(read_only=True) as uow:
            acc = resolve(uow, actor)
            m = self._see(uow, acc, memory_id)
            acc.require_edit(m, allow_active=self._auto_approve)
            if m.status != MemoryStatus.ARCHIVED.value:
                raise InvalidTransitionError(
                    "Only archived memories can be restored", {"status": m.status}
                )
            prev = uow.audit.pre_archive_status(memory_id)
        if prev is None:
            raise InvalidTransitionError("Pre-archive status unknown", {"memory_id": memory_id})
        return self.change_status(
            memory_id, StatusChange(to_status=MemoryStatus(prev), reason=reason), actor
        )

    def create_edge(self, cmd: EdgeCreate, actor: Actor) -> EdgeRead:
        with self._uow() as uow:
            self._guard_link(uow, resolve(uow, actor), cmd.from_memory_id, cmd.to_memory_id)
            result = self._graph.add_edge(uow, cmd, actor)
            uow.commit()
            return result

    def revoke_edge(self, edge_id: str, cmd: LinkRevoke, actor: Actor) -> EdgeRead:
        with self._uow() as uow:
            acc = resolve(uow, actor)
            edge = uow.edges.get(edge_id)
            if edge is None:
                raise NotFoundError("Edge not found", {"edge_id": edge_id})
            self._guard_link(uow, acc, edge.from_memory_id, edge.to_memory_id)
            result = edge_read(self._graph.revoke_edge(uow, edge, actor, cmd.reason))
            uow.commit()
            return result

    def list_edges(
        self, memory_id: str, include_revoked: bool = False, actor: Actor | None = None
    ) -> list[EdgeRead]:
        """History query: include_revoked=True also returns revoked edges."""
        with self._uow(read_only=True) as uow:
            acc = resolve(uow, actor)
            self._see(uow, acc, memory_id)
            rows = uow.edges.list_for(memory_id, include_revoked)
            ok = set(
                self._visible_ids(
                    uow, acc, [x for e in rows for x in (e.from_memory_id, e.to_memory_id)]
                )
            )
            return [edge_read(e) for e in rows if {e.from_memory_id, e.to_memory_id} <= ok]

    def create_relation(self, cmd: RelationCreate, actor: Actor) -> RelationRead:
        """CONTRADICTS is delegated to ConflictService.declare (agents may declare, Q-C4)."""
        with self._uow() as uow:
            self._guard_link(uow, resolve(uow, actor), cmd.from_memory_id, cmd.to_memory_id)
            rel, created = self._relations.add_relation(uow, cmd, actor)
            notice: str | None = None
            if created and cmd.relation_type is RelationType.CONTRADICTS:
                notice = self._conflicts.declare(uow, rel, actor)
            result = relation_read(rel, created, notice)
            uow.commit()
            return result

    def revoke_relation(self, relation_id: str, cmd: LinkRevoke, actor: Actor) -> RelationRead:
        with self._uow() as uow:
            acc = resolve(uow, actor)
            existing = uow.relations.get(relation_id)
            if acc.is_agent:
                if existing is None:
                    raise NotFoundError("Relation not found", {"relation_id": relation_id})
                self._guard_link(uow, acc, existing.from_memory_id, existing.to_memory_id)
            if existing is not None and existing.relation_type == RelationType.CONTRADICTS.value:
                rel, notice = self._conflicts.revoke_contradiction(
                    uow, relation_id, actor, cmd.reason
                )
            else:
                rel, notice = (
                    self._relations.revoke_relation(uow, relation_id, actor, cmd.reason),
                    None,
                )
            result = relation_read(rel, notice=notice)
            uow.commit()
            return result

    def resolve_conflict(
        self, conflict_id: str, cmd: ConflictResolve, actor: Actor
    ) -> ConflictRead:
        with self._uow() as uow:
            result = self._conflicts.resolve(uow, conflict_id, cmd, actor)
            uow.commit()
            return result

    def list_conflicts(
        self, memory_id: str, include_resolved: bool = False, actor: Actor | None = None
    ) -> list[ConflictRead]:
        with self._uow(read_only=True) as uow:
            acc = resolve(uow, actor)
            self._see(uow, acc, memory_id)
            rows = uow.conflicts.list_for(memory_id, include_resolved)
            ok = set(
                self._visible_ids(
                    uow, acc, [x for c in rows for x in (c.memory_a_id, c.memory_b_id)]
                )
            )
            visible = [c for c in rows if {c.memory_a_id, c.memory_b_id} <= ok]
            suggestions = suggest_for_conflicts(uow, visible)
            return [conflict_read(c, suggestion=suggestions.get(c.conflict_id)) for c in visible]

    def list_relations(
        self, memory_id: str, include_revoked: bool = False, actor: Actor | None = None
    ) -> list[RelationRead]:
        with self._uow(read_only=True) as uow:
            acc = resolve(uow, actor)
            self._see(uow, acc, memory_id)
            rows = uow.relations.list_for(memory_id, include_revoked)
            ok = set(
                self._visible_ids(
                    uow, acc, [x for r in rows for x in (r.from_memory_id, r.to_memory_id)]
                )
            )
            return [relation_read(r) for r in rows if {r.from_memory_id, r.to_memory_id} <= ok]

    def list_memory_ids_by_tag(self, tag: str) -> list[str]:
        with self._uow(read_only=True) as uow:
            return uow.tags.memory_ids_with(tag)

    _LINKED_ID_KEYS = (
        "from_memory_id", "to_memory_id", "superseded_by", "memory_a_id", "memory_b_id",
        "source_memory_ids", "output_memory_id",
    )  # fmt: skip

    def list_audit(self, memory_id: str, actor: Actor | None = None) -> list[AuditEventRead]:
        """Phase 4 L3: for agents, ids of memories they cannot see are redacted (set to null in
        memory_id / related_memory_id / before / after; entity_id -> "hidden") and the event
        carries related_hidden=true. Users and system see everything unchanged."""
        with self._uow(read_only=True) as uow:
            acc = resolve(uow, actor)
            self._see(uow, acc, memory_id)
            cache: dict[str, bool] = {}

            def hidden(mid: str | None) -> bool:
                if mid is None or acc.readable is None:
                    return False
                if mid not in cache:
                    m = uow.memories.get(mid)
                    cache[mid] = m is None or not acc.can_read(m.scope_id)
                return cache[mid]

            out: list[AuditEventRead] = []
            for e in uow.audit.list_for(memory_id):
                before = json.loads(e.before_json) if e.before_json else None
                after = json.loads(e.after_json) if e.after_json else None
                mem_id, rel_id, entity_id = e.memory_id, e.related_memory_id, e.entity_id
                red = False
                if hidden(mem_id):
                    mem_id, red = None, True
                if hidden(rel_id):
                    rel_id, red = None, True
                if e.entity_type == AuditEntityType.MEMORY.value and hidden(entity_id):
                    entity_id, red = "hidden", True
                for payload in (before, after):
                    if not isinstance(payload, dict):
                        continue
                    for k in self._LINKED_ID_KEYS:
                        v = payload.get(k)
                        if isinstance(v, str) and hidden(v):
                            payload[k], red = None, True
                        elif isinstance(v, list) and any(hidden(x) for x in v):
                            payload[k] = [None if hidden(x) else x for x in v]
                            red = True
                out.append(
                    AuditEventRead.model_validate(
                        {
                            "event_id": e.event_id,
                            "seq": e.seq,
                            "event_type": e.event_type,
                            "entity_type": e.entity_type,
                            "entity_id": entity_id,
                            "memory_id": mem_id,
                            "related_memory_id": rel_id,
                            "actor_type": e.actor_type,
                            "actor_id": e.actor_id,
                            "occurred_at": e.occurred_at,
                            "reason": e.reason,
                            "before": before,
                            "after": after,
                            "correlation_id": e.correlation_id,
                            "related_hidden": red,
                        }  # fmt: skip
                    )
                )
            return out

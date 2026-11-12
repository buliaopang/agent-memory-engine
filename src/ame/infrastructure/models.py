"""SQLAlchemy 2.x ORM models. Column names follow ADR-002 canonical naming."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _in(col: str, values: list[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


MEMORY_TYPES = ["working", "episodic", "semantic", "procedural"]
LAYERS = ["L1", "L2", "L3", "L4"]
SCOPE_TYPES = ["private", "project", "shared"]
STATUSES = ["candidate", "active", "conflicted", "superseded", "archived", "rejected"]
EDGE_TYPES = ["PARENT", "REFERENCE", "SUPERSEDES", "DERIVED_FROM"]
RELATION_TYPES = ["CONTRADICTS", "SUPPORTS", "RELATED_TO"]
LINK_STATUSES = ["active", "revoked"]
SOURCE_TYPES = ["conversation", "file", "task", "manual", "tool"]
ACTOR_TYPES = ["user", "agent", "system"]
LEGACY_AUDIT_EVENT_TYPES = [
    "created",
    "status_changed",
    "classification_changed",
    "version_created",
]
AUDIT_EVENT_TYPES = [
    "MEMORY_CREATED",
    "MEMORY_UPDATED",
    "MEMORY_VERSION_CREATED",
    "MEMORY_STATUS_CHANGED",
    "MEMORY_RESTORED",
    "EDGE_CREATED",
    "EDGE_REVOKED",
    "RELATION_CREATED",
    "RELATION_REVOKED",
    "CONFLICT_OPENED",
    "CONFLICT_RESOLVED",
    "SPACE_CREATED",
    "AGENT_REGISTERED",
    "AGENT_DISABLED",
    "AGENT_ENABLED",
    "AGENT_CREDENTIAL_ISSUED",
    "SPACE_MEMBER_GRANTED",
    "SPACE_MEMBER_REVOKED",
    "FEEDBACK_ADDED",
    "MEMORY_PROMOTED",
    "MEMORY_REJECTED",
    "DISTILL_CREATED",
    "EPOCH_CREATED",
    "EPOCH_CLOSED",
    "EPOCH_MEMBER_ADDED",
    "EPOCH_MEMBER_REMOVED",
]
AUDIT_ENTITY_TYPES = [
    "memory", "version", "edge", "relation", "conflict", "space", "agent",
    "feedback", "epoch", "distill",
]  # fmt: skip
FEEDBACK_KINDS = ["useful", "not_useful", "outdated", "wrong"]
CONFLICT_STATUSES = ["open", "resolved"]
CONFLICT_RESOLUTIONS = [
    "kept_both",
    "a_superseded_by_b",
    "b_superseded_by_a",
    "dismissed",
    "relation_revoked",
]


class AmeMeta(Base):
    __tablename__ = "ame_meta"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Memory(Base):
    """Memory header. Body lives only in immutable memory_versions."""

    __tablename__ = "memories"
    memory_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    layer: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memories_importance"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memories_confidence"),
        CheckConstraint("current_version >= 1", name="ck_memories_current_version"),
        CheckConstraint(_in("memory_type", MEMORY_TYPES), name="ck_memories_memory_type"),
        CheckConstraint(_in("layer", LAYERS), name="ck_memories_layer"),
        CheckConstraint(_in("scope_type", SCOPE_TYPES), name="ck_memories_scope_type"),
        CheckConstraint(_in("status", STATUSES), name="ck_memories_status"),
        Index("ix_memories_scope", "scope_type", "scope_id", "status", "layer"),
        Index("ix_memories_created", "created_by", "created_at"),
        Index("ix_memories_type", "memory_type"),
    )


class MemoryVersion(Base):
    """Append-only (DB triggers forbid UPDATE/DELETE)."""

    __tablename__ = "memory_versions"
    version_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("memory_id", "version", name="uq_memory_versions_memory_version"),
        CheckConstraint("version >= 1", name="ck_memory_versions_version"),
    )


_REVOKE_CONSISTENT = (
    "(status = 'active' AND revoked_at IS NULL AND revoked_by IS NULL "
    "AND revoked_actor_type IS NULL AND revoked_reason IS NULL) OR "
    "(status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
    "AND revoked_actor_type IS NOT NULL AND revoked_reason IS NOT NULL)"
)


class MemoryEdge(Base):
    """Structural DAG edge. Only status='active' edges take part in the DAG (ADR-006)."""

    __tablename__ = "memory_edges"
    edge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    from_memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    to_memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_actor_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("from_memory_id <> to_memory_id", name="ck_memory_edges_no_self_loop"),
        CheckConstraint(_in("edge_type", EDGE_TYPES), name="ck_memory_edges_edge_type"),
        CheckConstraint(_in("status", LINK_STATUSES), name="ck_memory_edges_status"),
        CheckConstraint(_REVOKE_CONSISTENT, name="ck_memory_edges_revoke_fields"),
        Index(
            "uq_memory_edges_active_triple",
            "from_memory_id",
            "to_memory_id",
            "edge_type",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_memory_edges_from", "from_memory_id", "edge_type", "status"),
        Index("ix_memory_edges_to", "to_memory_id", "edge_type", "status"),
        Index(
            "ix_memory_edges_active_adj",
            "from_memory_id",
            "to_memory_id",
            sqlite_where=text("status = 'active'"),
        ),
        # 0016 (ADR-033): single active PARENT per child -> parent tree
        Index(
            "ix_memory_edges_active_parent_child",
            "to_memory_id",
            sqlite_where=text("status = 'active' AND edge_type = 'PARENT'"),
        ),
    )


class MemoryRelation(Base):
    """Semantic relation (ADR-007/008). Never cycle-checked.

    Directed types (SUPPORTS) keep from->to as given; undirected types (CONTRADICTS,
    RELATED_TO) are normalized so that from_memory_id < to_memory_id.
    """

    __tablename__ = "memory_relations"
    relation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    to_memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_actor_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("from_memory_id <> to_memory_id", name="ck_memory_relations_no_self"),
        CheckConstraint(
            "relation_type = 'SUPPORTS' OR from_memory_id < to_memory_id",
            name="ck_memory_relations_normalized",
        ),
        CheckConstraint(
            _in("relation_type", RELATION_TYPES), name="ck_memory_relations_relation_type"
        ),
        CheckConstraint(_in("status", LINK_STATUSES), name="ck_memory_relations_status"),
        CheckConstraint(_REVOKE_CONSISTENT, name="ck_memory_relations_revoke_fields"),
        Index(
            "uq_memory_relations_active_pair",
            "relation_type",
            "from_memory_id",
            "to_memory_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_memory_relations_from", "from_memory_id", "status"),
        Index("ix_memory_relations_to", "to_memory_id", "status"),
    )


class MemoryTag(Base):
    __tablename__ = "memory_tags"
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    __table_args__ = (Index("ix_memory_tags_tag", "tag"),)


class MemorySource(Base):
    __tablename__ = "memory_sources"
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(_in("source_type", SOURCE_TYPES), name="ck_memory_sources_source_type"),
        Index("ix_memory_sources_memory", "memory_id"),
    )


class MemoryAuditEvent(Base):
    """LEGACY (0002). Frozen read-only since 0005: a trigger rejects new rows (ADR-013).

    History is kept and was back-filled into audit_events as event_id 'legacy_' || audit_id.
    """

    __tablename__ = "memory_audit_events"
    audit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_value: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(_in("event_type", LEGACY_AUDIT_EVENT_TYPES), name="ck_audit_event_type"),
        CheckConstraint(_in("actor_type", ACTOR_TYPES), name="ck_audit_actor_type"),
        Index("ix_memory_audit_events_memory", "memory_id", "created_at"),
    )


_RESOLVE_CONSISTENT = (
    "(status = 'open' AND resolved_at IS NULL AND resolved_by IS NULL "
    "AND resolved_actor_type IS NULL AND resolution IS NULL AND resolution_reason IS NULL "
    "AND resolution_evidence_json IS NULL) OR "
    "(status = 'resolved' AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL "
    "AND resolved_actor_type IS NOT NULL AND resolution IS NOT NULL "
    "AND resolution_reason IS NOT NULL AND resolution_evidence_json IS NOT NULL)"
)


class MemoryConflict(Base):
    """A substantive, adjudicable conflict (ADR-012).

    A CONTRADICTS relation is the *semantic* claim; a conflict row is the *operational*
    "needs a decision" record. conflicted status requires conflict evidence (DB trigger).
    """

    __tablename__ = "memory_conflicts"
    conflict_id: Mapped[str] = mapped_column(Text, primary_key=True)
    relation_id: Mapped[str] = mapped_column(
        ForeignKey("memory_relations.relation_id"), nullable=False
    )
    memory_a_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    memory_b_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[str] = mapped_column(Text, nullable=False)
    opened_by: Mapped[str] = mapped_column(Text, nullable=False)
    opened_actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_actor_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("memory_a_id < memory_b_id", name="ck_memory_conflicts_normalized"),
        CheckConstraint(_in("status", CONFLICT_STATUSES), name="ck_memory_conflicts_status"),
        CheckConstraint(
            "resolution IS NULL OR " + _in("resolution", CONFLICT_RESOLUTIONS),
            name="ck_memory_conflicts_resolution",
        ),
        CheckConstraint(
            "opened_actor_type IN ('user', 'agent', 'system') AND (resolved_actor_type IS NULL "
            "OR resolved_actor_type IN ('user', 'agent', 'system'))",
            name="ck_memory_conflicts_actor_type",
        ),
        CheckConstraint(_RESOLVE_CONSISTENT, name="ck_memory_conflicts_resolve_fields"),
        Index(
            "uq_memory_conflicts_open_relation",
            "relation_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
        ),
        Index("ix_memory_conflicts_a", "memory_a_id", "status"),
        Index("ix_memory_conflicts_b", "memory_b_id", "status"),
    )


class AuditEvent(Base):
    """Unified append-only audit log (ADR-013). DB triggers forbid UPDATE/DELETE.

    before_json/after_json hold state and metadata only - never memory content (Q-D2:
    content is represented by content_hash). memory_id has no FK on purpose so the audit
    trail is independent of entity tables.
    """

    __tablename__ = "audit_events"
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    memory_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_memory_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("seq", name="uq_audit_events_seq"),
        CheckConstraint(_in("event_type", AUDIT_EVENT_TYPES), name="ck_audit_events_event_type"),
        CheckConstraint(_in("entity_type", AUDIT_ENTITY_TYPES), name="ck_audit_events_entity_type"),
        CheckConstraint(_in("actor_type", ACTOR_TYPES), name="ck_audit_events_actor_type"),
        Index("ix_audit_events_memory", "memory_id", "seq"),
        Index("ix_audit_events_related", "related_memory_id", "seq"),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_correlation", "correlation_id"),
    )


class Agent(Base):
    """Agent registry row (ADR-018). credential_hash = sha256 of the agent key (ADR-024)."""

    __tablename__ = "agents"
    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    credential_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    credential_issued_at: Mapped[str | None] = mapped_column(Text, nullable=True)  # 0010
    disabled_at: Mapped[str | None] = mapped_column(Text, nullable=True)  # 0010
    __table_args__ = (
        CheckConstraint("enabled IN (0, 1)", name="ck_agents_enabled"),
        Index("ix_agents_credential_hash", "credential_hash"),  # 0012 (L5)
    )


class Space(Base):
    """Memory space (the scope_id of memories). DB triggers require memories to reference one."""

    __tablename__ = "spaces"
    space_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id"), nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint(_in("scope_type", SCOPE_TYPES), name="ck_spaces_scope_type"),)


class SpaceMember(Base):
    __tablename__ = "space_members"
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.space_id"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[str | None] = mapped_column(Text, nullable=True)  # 0010
    granted_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'editor', 'reader')", name="ck_space_members_role"),
    )


class OutboxEvent(Base):
    """Outbox (ADR-021). Rows are written by DB triggers in the same transaction as the change;
    payload holds ids only. Rows cannot be deleted."""

    __tablename__ = "outbox_events"
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')", name="ck_outbox_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        Index("ix_outbox_events_status_next", "status", "next_attempt_at"),
    )


class VectorIndexState(Base):
    """State of the derived vector index per memory (no text, no vectors; ADR-021)."""

    __tablename__ = "vector_index_state"
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "status IN ('indexed', 'failed', 'removed', 'disabled')",
            name="ck_vector_index_state_status",
        ),
    )


class IdempotencyKey(Base):
    """Replay cache for Idempotency-Key (ADR-027, 0011). Not truth; rows expire and are purged."""

    __tablename__ = "idempotency_keys"
    actor_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idem_key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'done')", name="ck_idempotency_state"),
        CheckConstraint(
            "(state = 'pending' AND status_code IS NULL AND response_body IS NULL) OR "
            "(state = 'done' AND status_code IS NOT NULL AND response_body IS NOT NULL)",
            name="ck_idempotency_done",
        ),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )


class MemoryFeedback(Base):
    """Append-only feedback (0013, ADR-029). Only the latest row per (memory, actor) counts."""

    __tablename__ = "memory_feedback"
    feedback_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    feedback: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(_in("feedback", FEEDBACK_KINDS), name="ck_memory_feedback_kind"),
        CheckConstraint(_in("actor_type", ACTOR_TYPES), name="ck_memory_feedback_actor_type"),
        Index("ix_memory_feedback_memory_actor", "memory_id", "actor_id", "created_at"),
    )


class Epoch(Base):
    """User-defined period (0014, ADR-030). closed is final; never deleted."""

    __tablename__ = "epochs"
    epoch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    space_id: Mapped[str | None] = mapped_column(ForeignKey("spaces.space_id"), nullable=True)
    start_at: Mapped[str] = mapped_column(Text, nullable=False)
    end_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="ck_epochs_status"),
        CheckConstraint(
            "(status = 'open' AND end_at IS NULL) OR (status = 'closed' AND end_at IS NOT NULL)",
            name="ck_epochs_closed_end",
        ),
    )


class EpochMember(Base):
    __tablename__ = "epoch_members"
    epoch_id: Mapped[str] = mapped_column(ForeignKey("epochs.epoch_id"), primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), primary_key=True)
    added_at: Mapped[str] = mapped_column(Text, nullable=False)
    added_by: Mapped[str] = mapped_column(Text, nullable=False)
    removed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    removed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "(removed_at IS NULL AND removed_by IS NULL) OR "
            "(removed_at IS NOT NULL AND removed_by IS NOT NULL)",
            name="ck_epoch_members_removed",
        ),
        Index("ix_epoch_members_memory", "memory_id"),
    )


class DistillJob(Base):
    """Append-only record of one rule-based distill draft (0015, ADR-032)."""

    __tablename__ = "distill_jobs"
    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_key: Mapped[str] = mapped_column(Text, nullable=False)
    output_memory_id: Mapped[str] = mapped_column(ForeignKey("memories.memory_id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('created')", name="ck_distill_jobs_status"),
        UniqueConstraint("rule", "input_key", name="uq_distill_jobs_input"),
    )

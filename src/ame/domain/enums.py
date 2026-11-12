"""Canonical enumerations (ADR-001/003/004)."""

from __future__ import annotations

from enum import StrEnum


class MemoryType(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class Layer(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class SpaceRole(StrEnum):
    """space_members.role (Phase 3a, ADR-025): reader = read; editor/owner = read + write."""

    OWNER = "owner"
    EDITOR = "editor"
    READER = "reader"


class ScopeType(StrEnum):
    PRIVATE = "private"
    PROJECT = "project"
    SHARED = "shared"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class EdgeType(StrEnum):
    """Structural DAG edge types (cycle-checked)."""

    PARENT = "PARENT"
    REFERENCE = "REFERENCE"
    SUPERSEDES = "SUPERSEDES"
    DERIVED_FROM = "DERIVED_FROM"


class RelationType(StrEnum):
    """Semantic relation types (NOT cycle-checked). ADR-007/008."""

    CONTRADICTS = "CONTRADICTS"  # undirected, normalized
    SUPPORTS = "SUPPORTS"  # directed: evidence (from) -> supported claim (to)
    RELATED_TO = "RELATED_TO"  # undirected, normalized


DIRECTED_RELATION_TYPES = frozenset({RelationType.SUPPORTS})


class LinkStatus(StrEnum):
    """Lifecycle of structural edges and semantic relations (ADR-006)."""

    ACTIVE = "active"
    REVOKED = "revoked"


class SourceType(StrEnum):
    CONVERSATION = "conversation"
    FILE = "file"
    TASK = "task"
    MANUAL = "manual"
    TOOL = "tool"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class LegacyAuditEventType(StrEnum):
    """Event types of the frozen, read-only memory_audit_events table (ADR-013)."""

    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    CLASSIFICATION_CHANGED = "classification_changed"
    VERSION_CREATED = "version_created"


class AuditEventType(StrEnum):
    """Event types of the unified append-only audit_events table (ADR-013)."""

    MEMORY_CREATED = "MEMORY_CREATED"
    MEMORY_UPDATED = "MEMORY_UPDATED"
    MEMORY_VERSION_CREATED = "MEMORY_VERSION_CREATED"
    MEMORY_STATUS_CHANGED = "MEMORY_STATUS_CHANGED"
    MEMORY_RESTORED = "MEMORY_RESTORED"
    EDGE_CREATED = "EDGE_CREATED"
    EDGE_REVOKED = "EDGE_REVOKED"
    RELATION_CREATED = "RELATION_CREATED"
    RELATION_REVOKED = "RELATION_REVOKED"
    CONFLICT_OPENED = "CONFLICT_OPENED"
    CONFLICT_RESOLVED = "CONFLICT_RESOLVED"
    # Phase 3a (migration 0009, ADR-025)
    SPACE_CREATED = "SPACE_CREATED"
    AGENT_REGISTERED = "AGENT_REGISTERED"
    AGENT_DISABLED = "AGENT_DISABLED"
    AGENT_ENABLED = "AGENT_ENABLED"
    AGENT_CREDENTIAL_ISSUED = "AGENT_CREDENTIAL_ISSUED"
    SPACE_MEMBER_GRANTED = "SPACE_MEMBER_GRANTED"
    SPACE_MEMBER_REVOKED = "SPACE_MEMBER_REVOKED"
    # Phase 4 (migration 0013, ADR-029..032)
    FEEDBACK_ADDED = "FEEDBACK_ADDED"
    MEMORY_PROMOTED = "MEMORY_PROMOTED"
    MEMORY_REJECTED = "MEMORY_REJECTED"  # review queue reject (carries draft sources)
    DISTILL_CREATED = "DISTILL_CREATED"
    EPOCH_CREATED = "EPOCH_CREATED"
    EPOCH_CLOSED = "EPOCH_CLOSED"
    EPOCH_MEMBER_ADDED = "EPOCH_MEMBER_ADDED"
    EPOCH_MEMBER_REMOVED = "EPOCH_MEMBER_REMOVED"


class AuditEntityType(StrEnum):
    MEMORY = "memory"
    VERSION = "version"
    EDGE = "edge"
    RELATION = "relation"
    CONFLICT = "conflict"
    SPACE = "space"  # Phase 3a
    AGENT = "agent"  # Phase 3a
    FEEDBACK = "feedback"  # Phase 4
    EPOCH = "epoch"  # Phase 4
    DISTILL = "distill"  # Phase 4


class FeedbackKind(StrEnum):
    """memory_feedback.feedback (Phase 4, ADR-029)."""

    USEFUL = "useful"
    NOT_USEFUL = "not_useful"
    OUTDATED = "outdated"
    WRONG = "wrong"


class EpochStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ConflictStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ConflictResolution(StrEnum):
    """How a substantive conflict was closed (ADR-012)."""

    KEPT_BOTH = "kept_both"  # user: both stay valid, CONTRADICTS relation is kept
    A_SUPERSEDED_BY_B = "a_superseded_by_b"
    B_SUPERSEDED_BY_A = "b_superseded_by_a"
    DISMISSED = "dismissed"  # user: contradiction is not substantive
    RELATION_REVOKED = "relation_revoked"  # the CONTRADICTS relation itself was revoked


USER_RESOLUTIONS = frozenset(
    {
        ConflictResolution.KEPT_BOTH,
        ConflictResolution.A_SUPERSEDED_BY_B,
        ConflictResolution.B_SUPERSEDED_BY_A,
        ConflictResolution.DISMISSED,
    }
)

# Q-N11: no new active edges/relations may touch memories in these states.
FROZEN_LINK_STATUSES = frozenset({MemoryStatus.ARCHIVED, MemoryStatus.REJECTED})

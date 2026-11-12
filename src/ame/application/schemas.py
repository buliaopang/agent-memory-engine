"""Pydantic v2 DTOs. Field names follow ADR-002 and match DB columns 1:1 where shared."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ame.domain.enums import (
    ActorType,
    AuditEntityType,
    AuditEventType,
    ConflictResolution,
    ConflictStatus,
    EdgeType,
    EpochStatus,
    FeedbackKind,
    Layer,
    LinkStatus,
    MemoryStatus,
    MemoryType,
    RelationType,
    ScopeType,
    SourceType,
    SpaceRole,
)

# ADR-005: core fields/relations must never be smuggled into metadata.
RESERVED_METADATA_KEYS = frozenset(
    {
        "memory_id", "version_id", "current_version", "version", "edge_id", "relation_id",
        "source_id", "content", "summary", "memory_type", "layer", "scope_type", "scope_id",
        "status", "importance", "confidence", "tags", "sources", "parent_ids",
        "reference_ids", "created_by", "created_at", "updated_at", "content_hash",
    }
)  # fmt: skip


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_metadata(v: dict[str, Any]) -> dict[str, Any]:
    bad = RESERVED_METADATA_KEYS & set(v)
    if bad:
        raise ValueError(f"metadata must not contain core fields: {sorted(bad)}")
    return v


class Actor(_Strict):
    """Caller identity. Phase 3 binds this to authenticated credentials."""

    actor_type: ActorType
    actor_id: str = Field(min_length=1)


class SourceCreate(_Strict):
    source_type: SourceType
    source_ref: str | None = None
    excerpt: str | None = None
    content_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceRead(_Strict):
    source_id: str
    memory_id: str
    source_type: SourceType
    source_ref: str | None
    excerpt: str | None
    content_hash: str | None
    created_at: str
    metadata: dict[str, Any]


class MemoryCreate(_Strict):
    content: str = Field(min_length=1)
    summary: str | None = None
    memory_type: MemoryType
    layer: Layer
    scope_type: ScopeType
    scope_id: str = Field(min_length=1)
    importance: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    tags: list[str] = Field(default_factory=list)
    parent_ids: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)
    sources: list[SourceCreate] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _meta = field_validator("metadata")(_check_metadata)

    @field_validator("parent_ids")
    @classmethod
    def _one_parent(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for pid in v:
            if pid not in out:
                out.append(pid)
        if len(out) > 1:
            raise ValueError(
                "at most one parent_id is allowed (Conflux parent tree, ADR-033); "
                "pass extra predecessors in reference_ids"
            )
        return out

    @field_validator("tags")
    @classmethod
    def _norm_tags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = t.strip()
            if not t or len(t) > 64:
                raise ValueError("tag must be 1..64 chars")
            if t not in out:
                out.append(t)
        return out


class MemoryRead(_Strict):
    memory_id: str
    current_version: int
    version_id: str
    content: str
    summary: str | None
    content_hash: str
    memory_type: MemoryType
    layer: Layer
    scope_type: ScopeType
    scope_id: str
    created_by: str
    created_at: str
    updated_at: str
    importance: float
    confidence: float
    status: MemoryStatus
    tags: list[str]
    sources: list[SourceRead]
    parent_ids: list[str]
    reference_ids: list[str]
    metadata: dict[str, Any]
    # Phase 2 (derived index, ADR-021): pending|indexed|stale|failed|removed|disabled
    vector_status: str = "pending"


class VersionCreate(_Strict):
    content: str = Field(min_length=1)
    summary: str | None = None
    change_reason: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class VersionRead(_Strict):
    version_id: str
    memory_id: str
    version: int
    content: str
    summary: str | None
    content_hash: str
    created_by: str
    created_at: str
    change_reason: str | None


class EdgeCreate(_Strict):
    """Structural DAG edge (PARENT/REFERENCE/SUPERSEDES/DERIVED_FROM)."""

    from_memory_id: str
    to_memory_id: str
    edge_type: EdgeType
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinkRevoke(_Strict):
    """Revoke a structural edge or semantic relation (ADR-006/008)."""

    reason: str = Field(min_length=1)


class EdgeRead(_Strict):
    edge_id: str
    from_memory_id: str
    to_memory_id: str
    edge_type: EdgeType
    status: LinkStatus
    created_by: str
    created_at: str
    metadata: dict[str, Any]
    revoked_at: str | None
    revoked_by: str | None
    revoked_actor_type: ActorType | None
    revoked_reason: str | None
    created: bool


class RelationCreate(_Strict):
    """Semantic relation. SUPPORTS is directed (from = evidence, to = supported);
    CONTRADICTS / RELATED_TO are undirected and normalized by the service."""

    from_memory_id: str
    to_memory_id: str
    relation_type: RelationType
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _distinct(self) -> RelationCreate:
        if self.from_memory_id == self.to_memory_id:
            raise ValueError("a memory cannot relate to itself")
        return self


class RelationRead(_Strict):
    relation_id: str
    relation_type: RelationType
    directed: bool
    from_memory_id: str
    to_memory_id: str
    status: LinkStatus
    created_by: str
    created_at: str
    metadata: dict[str, Any]
    revoked_at: str | None
    revoked_by: str | None
    revoked_actor_type: ActorType | None
    revoked_reason: str | None
    created: bool = False
    review_notice: str | None = None


class StatusChange(_Strict):
    to_status: MemoryStatus
    reason: str = Field(min_length=1)
    superseded_by_memory_id: str | None = None


class ClassificationChange(_Strict):
    layer: Layer | None = None
    memory_type: MemoryType | None = None
    reason: str | None = None


class AuditEventRead(_Strict):
    """Unified audit event (ADR-013). before/after never contain memory content."""

    event_id: str
    seq: int
    event_type: AuditEventType
    entity_type: AuditEntityType
    entity_id: str
    memory_id: str | None
    related_memory_id: str | None
    actor_type: ActorType
    actor_id: str
    occurred_at: str
    reason: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    correlation_id: str
    # Phase 4 L3: true when ids of memories the caller cannot see were redacted to null.
    related_hidden: bool = False


class ConflictResolve(_Strict):
    """User-only resolution of a substantive conflict (Q-C4). evidence is mandatory."""

    resolution: ConflictResolution
    reason: str = Field(min_length=1)
    evidence: dict[str, Any]

    @field_validator("resolution")
    @classmethod
    def _user_resolution(cls, v: ConflictResolution) -> ConflictResolution:
        if v is ConflictResolution.RELATION_REVOKED:
            raise ValueError("relation_revoked is set only by revoking the CONTRADICTS relation")
        return v


class ConflictSuggestion(_Strict):
    """Conflux total-order 'first-arrived wins' suggestion (ADR-033). Never auto-applied."""

    winner_memory_id: str
    loser_memory_id: str
    recommended_resolution: ConflictResolution
    winner_rank: int
    loser_rank: int
    winner_epoch: int
    loser_epoch: int
    winner_pivot_memory_id: str | None
    loser_pivot_memory_id: str | None
    explanation: str


class ConflictRead(_Strict):
    conflict_id: str
    relation_id: str
    memory_a_id: str
    memory_b_id: str
    status: ConflictStatus
    opened_at: str
    opened_by: str
    opened_actor_type: ActorType
    evidence: dict[str, Any]
    resolved_at: str | None
    resolved_by: str | None
    resolved_actor_type: ActorType | None
    resolution: ConflictResolution | None
    resolution_reason: str | None
    resolution_evidence: dict[str, Any] | None
    notice: str | None = None
    suggestion: ConflictSuggestion | None = None


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$"


class SpaceCreate(_Strict):
    """Create a space (ADR-018). Membership/ACL is Phase 3."""

    space_id: str = Field(pattern=_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    scope_type: ScopeType
    owner_agent_id: str | None = None


class SpaceRead(_Strict):
    space_id: str
    name: str
    scope_type: ScopeType
    owner_agent_id: str | None
    created_at: str


class AgentCreate(_Strict):
    """Register an agent row only; no credential is issued before Phase 3 (ADR-018)."""

    agent_id: str = Field(pattern=_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    agent_type: str = Field(min_length=1, max_length=64)


class AgentRead(_Strict):
    agent_id: str
    display_name: str
    agent_type: str
    enabled: bool
    has_credential: bool
    created_at: str


class AgentCredential(_Strict):
    """Returned ONCE when the user issues/rotates a key (ADR-024). Never stored in plaintext."""

    agent_id: str
    api_key: str
    issued_at: str


class AgentStateChange(_Strict):
    reason: str = Field(min_length=1, max_length=2000)


class SpaceGrant(_Strict):
    agent_id: str = Field(pattern=_ID_PATTERN)
    role: SpaceRole
    reason: str = Field(min_length=1, max_length=2000)


class SpaceMemberRead(_Strict):
    space_id: str
    agent_id: str
    role: str
    granted_at: str | None
    granted_by: str | None
    revoked_at: str | None
    revoked_by: str | None


class MemoryPatch(_Strict):
    """PATCH: mutable metadata only (importance, tags). Never content/status (API_SPEC)."""

    importance: float | None = Field(default=None, ge=0, le=1)
    tags: list[str] | None = None
    reason: str = Field(min_length=1)

    @field_validator("tags")
    @classmethod
    def _norm_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for t in v:
            t = t.strip()
            if not t or len(t) > 64:
                raise ValueError("tag must be 1..64 chars")
            if t not in out:
                out.append(t)
        return out


class ReasonBody(_Strict):
    """Body for archive / restore (reason is mandatory, I-5)."""

    reason: str = Field(min_length=1)


class GraphOrder(_Strict):
    """Topological order of memories over ACTIVE structural edges (ADR-019)."""

    memory_ids: list[str]
    node_count: int
    edge_count: int
    truncated: bool
    edge_types: list[EdgeType]


# ---------- Phase 2: search (ADR-020) ----------
class SearchRequest(_Strict):
    query: str = Field(min_length=1, max_length=2000)
    # Phase 4 (ADR-030): restrict results to the active members of one epoch.
    epoch_id: str | None = None
    memory_types: list[MemoryType] = Field(default_factory=list)
    scope_ids: list[str] = Field(default_factory=list, max_length=100)
    layers: list[Layer] = Field(default_factory=list)
    top_k: int = Field(default=10, ge=1, le=100)
    include_graph_context: bool = False
    include_sources: bool = True
    include_conflicts: bool = False


class ScoreBreakdown(_Strict):
    vector: float | None  # None = not used / not available (never a made-up number)
    keyword: float
    graph: float
    importance: float
    confidence: float
    freshness: float
    weights: dict[str, float]
    vector_used: bool


class GraphContextItem(_Strict):
    memory_id: str
    kind: Literal["edge", "relation"]
    link_type: str
    direction: Literal["out", "in"]


class SearchResult(_Strict):
    memory: MemoryRead
    score: float
    score_breakdown: ScoreBreakdown
    matched_terms: int
    conflicted: bool
    graph_context: list[GraphContextItem]


class SearchResponse(_Strict):
    query_id: str
    retrieval_mode: Literal["hybrid", "lexical_graph"]
    degraded_reason: str | None
    total_candidates: int
    results: list[SearchResult]


# ---------- Phase 2: graph expansion and context packing ----------
class GraphNode(_Strict):
    memory_id: str
    memory_type: MemoryType
    layer: Layer
    scope_id: str
    status: MemoryStatus
    summary: str | None
    depth: int


class GraphLink(_Strict):
    kind: Literal["edge", "relation"]
    link_id: str
    link_type: str
    from_memory_id: str
    to_memory_id: str


class GraphView(_Strict):
    center_id: str | None
    nodes: list[GraphNode]
    links: list[GraphLink]
    truncated: bool


class ContextRequest(_Strict):
    task_description: str = Field(min_length=1, max_length=4000)
    scope_ids: list[str] = Field(default_factory=list, max_length=100)
    layers: list[Layer] = Field(default_factory=list)
    memory_types: list[MemoryType] = Field(default_factory=list)
    token_budget: int = Field(default=2000, ge=50, le=32000)
    include_procedural: bool = True
    include_sources: bool = True


class ContextItem(_Strict):
    memory_id: str
    memory_type: MemoryType
    layer: Layer
    status: MemoryStatus
    score: float
    text: str
    truncated: bool
    tokens: int
    source_ids: list[str]


class ContextPack(_Strict):
    notice: str
    retrieval_mode: Literal["hybrid", "lexical_graph"]
    token_budget: int
    used_tokens: int
    items: list[ContextItem]
    omitted: int


class StatsRead(_Strict):
    memories: dict[str, int]  # counts by status (+ "total"); no content
    outbox: dict[str, int]
    vector_state: dict[str, int]
    retrieval_mode: Literal["hybrid", "lexical_graph"]


# ---------------- Phase 4 (ADR-029..032) ----------------
class FeedbackCreate(_Strict):
    """Field names equal the MCP memory_feedback input (memory_id comes from the path)."""

    feedback: FeedbackKind
    reason: str = Field(min_length=1, max_length=2000)


class FeedbackRead(_Strict):
    feedback_id: str
    memory_id: str
    actor_type: ActorType
    actor_id: str
    feedback: FeedbackKind
    reason: str
    created_at: str


class FeedbackSummary(_Strict):
    """Only the latest feedback per actor counts (anti-spam)."""

    memory_id: str
    counts: dict[str, int]
    voters: int
    score: float
    items: list[FeedbackRead]


class EpochCreate(_Strict):
    name: str = Field(min_length=1, max_length=200)
    space_id: str | None = None
    start_at: str | None = None
    reason: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EpochMemberChange(_Strict):
    memory_id: str
    reason: str = Field(min_length=1)


class EpochClose(_Strict):
    reason: str = Field(min_length=1)


class EpochRead(_Strict):
    epoch_id: str
    name: str
    space_id: str | None
    start_at: str
    end_at: str | None
    status: EpochStatus
    created_by: str
    created_at: str
    metadata: dict[str, Any]
    member_ids: list[str]


class DerivedEpochRead(_Strict):
    """Auto-derived Conflux epoch partitioned by a pivot chain memory (ADR-033)."""

    index: int
    pivot_memory_id: str | None
    memory_ids: list[str]


class SpineItem(_Strict):
    memory_id: str
    status: MemoryStatus
    scope_id: str
    summary: str | None
    score: float
    components: dict[str, float]
    weights: dict[str, float]
    explanation: list[str]
    parent_memory_id: str | None = None
    subtree_weight: float = 0.0
    on_pivot_chain: bool = False
    epoch_index: int | None = None
    total_order_rank: int | None = None


class SpineResponse(_Strict):
    total_candidates: int
    items: list[SpineItem]
    pivot_chain: list[str] = Field(default_factory=list)
    derived_epochs: list[DerivedEpochRead] = Field(default_factory=list)
    total_order: list[str] = Field(default_factory=list)


class DistillRequest(_Strict):
    scope_id: str
    tag: str | None = None
    threshold: float | None = Field(default=None, gt=0, le=1)
    reason: str = Field(min_length=1)


class DistillDraft(_Strict):
    job_id: str
    memory_id: str
    rule: str
    source_memory_ids: list[str]


class DistillResult(_Strict):
    created: list[DistillDraft]
    skipped_existing: int
    groups_considered: int


class PromoteRequest(_Strict):
    reason: str = Field(min_length=1)


class ReviewItem(_Strict):
    memory_id: str
    scope_id: str
    created_by: str
    created_at: str
    summary: str | None
    is_distill_draft: bool
    source_memory_ids: list[str]


class DecaySuggestion(_Strict):
    memory_id: str
    status: MemoryStatus
    scope_id: str
    score: float
    age_days: float
    importance: float
    feedback: float
    explanation: list[str]


class DecayResponse(_Strict):
    threshold: float
    half_life_days: float
    suggestions: list[DecaySuggestion]


class DecayArchive(_Strict):
    memory_id: str
    reason: str = Field(min_length=1)

"""Unit of Work: one transaction boundary per use case.

Write UoWs start with BEGIN IMMEDIATE (ADR-010, Q-A2); read-only UoWs use DEFERRED.
Every write UoW carries one correlation_id shared by all audit events it records.
SQLite trigger aborts are translated to domain errors here (one place).
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ame.application.ids import new_id
from ame.errors import (
    AMEError,
    ConflictEvidenceRequiredError,
    CycleDetectedError,
    EpochClosedError,
    ImmutableHistoryError,
    LinkEndpointFrozenError,
    MemoryArchivedError,
    NotFoundError,
    ParentAlreadyExistsError,
    SpaceNotFoundError,
    StorageError,
)
from ame.infrastructure.database import begin_mode
from ame.infrastructure.outbox_repository import OutboxRepository
from ame.infrastructure.phase4_repositories import (
    DistillRepository,
    EpochRepository,
    FeedbackRepository,
    Phase4ReadRepository,
)
from ame.infrastructure.repositories import (
    AgentRepository,
    AuditRepository,
    ConflictRepository,
    EdgeRepository,
    IdempotencyRepository,
    LegacyAuditRepository,
    MemoryRepository,
    RelationRepository,
    SourceRepository,
    SpaceRepository,
    TagRepository,
    VersionRepository,
)
from ame.infrastructure.search_repository import SearchRepository

_TRIGGER_ERRORS: tuple[tuple[str, type[AMEError]], ...] = (
    ("EPOCH_CLOSED", EpochClosedError),
    ("PARENT_ALREADY_EXISTS", ParentAlreadyExistsError),
    ("DAG_CYCLE_DETECTED", CycleDetectedError),
    ("SPACE_NOT_FOUND", SpaceNotFoundError),
    ("immutable", ImmutableHistoryError),
    ("EDGE_ENDPOINT_MISSING", NotFoundError),
    ("LINK_ENDPOINT_FROZEN", LinkEndpointFrozenError),
    ("CONFLICT_EVIDENCE_REQUIRED", ConflictEvidenceRequiredError),
    ("memory is archived", MemoryArchivedError),
    ("append-only", ImmutableHistoryError),
    ("only active->revoked", ImmutableHistoryError),
    ("only open->resolved", ImmutableHistoryError),
    ("is frozen", ImmutableHistoryError),
    ("delete not allowed", ImmutableHistoryError),
)


def translate_db_error(exc: BaseException) -> AMEError | None:
    if isinstance(exc, IntegrityError):
        msg = str(exc.orig)
        for marker, err in _TRIGGER_ERRORS:
            if marker in msg:
                return err(msg.split(" [SQL")[0], {"db_guard": marker})
        return None
    if isinstance(exc, OperationalError) and "locked" in str(exc.orig):
        return StorageError("Database is busy, retry later", {"db_guard": "busy"})
    return None


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


class UnitOfWork:
    memories: MemoryRepository
    versions: VersionRepository
    edges: EdgeRepository
    relations: RelationRepository
    conflicts: ConflictRepository
    tags: TagRepository
    sources: SourceRepository
    audit: AuditRepository
    legacy_audit: LegacyAuditRepository
    spaces: SpaceRepository
    agents: AgentRepository
    search: SearchRepository
    idempotency: IdempotencyRepository
    feedback: FeedbackRepository
    epochs: EpochRepository
    distill: DistillRepository
    p4: Phase4ReadRepository

    def __init__(self, session_factory: sessionmaker[Session], *, read_only: bool = False) -> None:
        self._factory = session_factory
        self.read_only = read_only
        self.correlation_id = new_id("cor")

    def __enter__(self) -> Self:
        self._session = self._factory()
        s = self._session
        with begin_mode("DEFERRED" if self.read_only else "IMMEDIATE"):
            s.connection()  # start the transaction now, with the chosen BEGIN mode
        self.memories = MemoryRepository(s)
        self.versions = VersionRepository(s)
        self.edges = EdgeRepository(s)
        self.relations = RelationRepository(s)
        self.conflicts = ConflictRepository(s)
        self.tags = TagRepository(s)
        self.sources = SourceRepository(s)
        self.audit = AuditRepository(s)
        self.legacy_audit = LegacyAuditRepository(s)
        self.spaces = SpaceRepository(s)
        self.agents = AgentRepository(s)
        self.search = SearchRepository(s)
        self.outbox = OutboxRepository(s)
        self.idempotency = IdempotencyRepository(s)
        self.feedback = FeedbackRepository(s)
        self.epochs = EpochRepository(s)
        self.distill = DistillRepository(s)
        self.p4 = Phase4ReadRepository(s)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._session.rollback()  # no-op after commit; discards anything uncommitted
        finally:
            self._session.close()
        if exc is not None:
            mapped = translate_db_error(exc)
            if mapped is not None:
                raise mapped from exc

    def commit(self) -> None:
        if self.read_only:
            raise RuntimeError("read-only unit of work cannot commit")
        self._session.commit()

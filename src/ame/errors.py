"""Application errors mapped to the unified API error envelope (API_SPEC.md)."""

from __future__ import annotations

from typing import Any


class AMEError(Exception):
    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AMEError):
    code = "NOT_FOUND"
    http_status = 404


class ValidationFailedError(AMEError):
    code = "VALIDATION_ERROR"
    http_status = 422


class VersionConflictError(AMEError):
    code = "VERSION_CONFLICT"
    http_status = 409


class InvalidTransitionError(AMEError):
    code = "INVALID_STATUS_TRANSITION"
    http_status = 409


class TransitionNotPermittedError(AMEError):
    code = "TRANSITION_NOT_PERMITTED"
    http_status = 403


class CycleDetectedError(AMEError):
    code = "DAG_CYCLE_DETECTED"
    http_status = 409


class ReasonRequiredError(AMEError):
    code = "REASON_REQUIRED"
    http_status = 422


class ImmutableHistoryError(AMEError):
    code = "IMMUTABLE_HISTORY"
    http_status = 409


class MemoryArchivedError(AMEError):
    code = "MEMORY_ARCHIVED"
    http_status = 409


class ConflictUnresolvedError(AMEError):
    """T6/T7 via change_status while open substantive conflicts remain (ADR-012)."""

    code = "CONFLICT_UNRESOLVED"
    http_status = 409


class ConflictEvidenceRequiredError(AMEError):
    code = "CONFLICT_EVIDENCE_REQUIRED"
    http_status = 409


class LinkEndpointFrozenError(AMEError):
    """Q-N11: archived/rejected memories cannot get new edges or relations."""

    code = "LINK_ENDPOINT_FROZEN"
    http_status = 409


class StorageError(AMEError):
    code = "STORAGE_ERROR"
    http_status = 500


class MigrationFailedError(AMEError):
    code = "MIGRATION_FAILED"
    http_status = 500


class SpaceNotFoundError(AMEError):
    """Memory references a scope_id/scope_type with no matching space (ADR-018)."""

    code = "SPACE_NOT_FOUND"
    http_status = 422


class AlreadyExistsError(AMEError):
    code = "ALREADY_EXISTS"
    http_status = 409


class AuthenticationRequiredError(AMEError):
    """Missing/invalid local API token (ADR-017). Never echoes the presented token."""

    code = "UNAUTHORIZED"
    http_status = 401


class NotSupportedError(AMEError):
    """Feature explicitly deferred (e.g. Idempotency-Key until Phase 3, ADR-017)."""

    code = "IDEMPOTENCY_NOT_SUPPORTED"
    http_status = 501


class PermissionDeniedError(TransitionNotPermittedError):
    """Phase 3a (ADR-024/025): caller can SEE the target but may not write it."""

    code = "PERMISSION_DENIED"
    http_status = 403


class IdempotencyKeyMismatchError(AMEError):
    """Phase 3b (ADR-027): same Idempotency-Key reused with a different request (IETF draft)."""

    code = "IDEMPOTENCY_KEY_MISMATCH"
    http_status = 422


class IdempotencyInProgressError(AMEError):
    """Phase 3b (ADR-027): the first request with this key has not finished yet; retry later."""

    code = "IDEMPOTENCY_IN_PROGRESS"
    http_status = 409


class McpDisabledError(AMEError):
    """Phase 3c (ADR-028): MCP channel switched off (AME_MCP_ENABLED / AME_MCP_READ_ONLY)."""

    code = "MCP_DISABLED"
    http_status = 403


class EpochClosedError(AMEError):
    """Phase 4 (ADR-030): a closed epoch is final; members cannot change."""

    code = "EPOCH_CLOSED"
    http_status = 409


class ParentAlreadyExistsError(AMEError):
    """ADR-033 (Conflux parent tree): a memory may have at most one active PARENT edge."""

    code = "PARENT_ALREADY_EXISTS"
    http_status = 409

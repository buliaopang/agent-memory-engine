"""Six-state memory lifecycle (ADR-003, docs/STATE_MACHINE.md)."""

from __future__ import annotations

from dataclasses import dataclass

from ame.domain.enums import ActorType, MemoryStatus
from ame.errors import InvalidTransitionError, TransitionNotPermittedError

S = MemoryStatus
U = frozenset({ActorType.USER})
UA = frozenset({ActorType.USER, ActorType.AGENT})
SYS = frozenset({ActorType.SYSTEM})


@dataclass(frozen=True)
class TransitionRule:
    code: str
    from_status: MemoryStatus
    to_status: MemoryStatus
    actors: frozenset[ActorType]
    trigger: str


_ARCHIVABLE = (S.CANDIDATE, S.ACTIVE, S.CONFLICTED, S.SUPERSEDED, S.REJECTED)

RULES: tuple[TransitionRule, ...] = (
    TransitionRule("T3", S.CANDIDATE, S.ACTIVE, U, "user approves candidate"),
    TransitionRule("T4", S.CANDIDATE, S.REJECTED, U, "user rejects candidate"),
    TransitionRule("T5", S.ACTIVE, S.CONFLICTED, SYS, "CONTRADICTS between two active memories"),
    TransitionRule("T6", S.CONFLICTED, S.ACTIVE, U, "user resolves conflict: keep"),
    TransitionRule("T7", S.CONFLICTED, S.SUPERSEDED, U, "user resolves conflict: replaced"),
    TransitionRule("T8", S.ACTIVE, S.SUPERSEDED, UA, "explicitly superseded by another memory"),
    *(TransitionRule("T9", s, S.ARCHIVED, UA, "archive (no physical delete)") for s in _ARCHIVABLE),
    *(
        TransitionRule("T10", S.ARCHIVED, s, U, "restore to pre-archive status")
        for s in _ARCHIVABLE
    ),
    TransitionRule("T11", S.SUPERSEDED, S.ACTIVE, U, "user revokes supersession"),
    TransitionRule("T12", S.REJECTED, S.CANDIDATE, UA, "resubmit rejected memory for review"),
)

_INDEX = {(r.from_status, r.to_status): r for r in RULES}


def initial_status(actor_type: ActorType) -> tuple[MemoryStatus, str]:
    """T1: agent/system writes start as candidate; T2: user manual entry is active."""
    if actor_type is ActorType.USER:
        return S.ACTIVE, "T2"
    return S.CANDIDATE, "T1"


def authorize_transition(
    from_status: MemoryStatus, to_status: MemoryStatus, actor_type: ActorType
) -> TransitionRule:
    rule = _INDEX.get((from_status, to_status))
    if rule is None:
        raise InvalidTransitionError(
            f"Transition {from_status} -> {to_status} is not allowed",
            {"from_status": str(from_status), "to_status": str(to_status)},
        )
    if actor_type not in rule.actors:
        raise TransitionNotPermittedError(
            f"Actor type {actor_type} may not perform {rule.code}",
            {"rule": rule.code, "actor_type": str(actor_type)},
        )
    return rule

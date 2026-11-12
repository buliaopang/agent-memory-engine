"""Phase 3a access policy (ADR-025). Pure reads; no writes, no audit.

Rules (PHASE_3_REVIEW decisions 4-7):
- user / system: unrestricted (``None`` = no filter).
- agent: must exist and be enabled. Readable = every ``shared`` space + spaces with an active
  (non-revoked) ``space_members`` grant. Writable = ``shared`` + active ``editor``/``owner``.
- Anything not readable is reported as 404 by callers (never 403: no existence leak).
"""

from __future__ import annotations

from dataclasses import dataclass

from ame.application.schemas import Actor
from ame.domain.enums import ActorType
from ame.errors import AuthenticationRequiredError, NotFoundError, PermissionDeniedError
from ame.infrastructure.models import Memory
from ame.infrastructure.unit_of_work import UnitOfWork

WRITE_ROLES = frozenset({"editor", "owner"})


def agent_actor(agent_id: str) -> Actor:
    """An authenticated agent acts as actor_id == its registry agent_id (e.g. 'agent:codex')."""
    return Actor(actor_type=ActorType.AGENT, actor_id=agent_id)


def agent_id_of(actor: Actor) -> str:
    return actor.actor_id


@dataclass(frozen=True)
class Access:
    """Resolved space sets for one actor inside one transaction. ``None`` = unrestricted."""

    readable: frozenset[str] | None
    writable: frozenset[str] | None
    actor: Actor | None

    @property
    def is_agent(self) -> bool:
        return self.actor is not None and self.actor.actor_type is ActorType.AGENT

    def can_read(self, space_id: str) -> bool:
        return self.readable is None or space_id in self.readable

    def can_write(self, space_id: str) -> bool:
        return self.writable is None or space_id in self.writable

    def see_memory(self, m: Memory | None, memory_id: str) -> Memory:
        if m is None or not self.can_read(m.scope_id):
            raise NotFoundError("Memory not found", {"memory_id": memory_id})
        return m

    def require_write_space(self, space_id: str) -> None:
        if not self.can_read(space_id):
            raise NotFoundError("Space not found", {"space_id": space_id})
        if not self.can_write(space_id):
            raise PermissionDeniedError("No write access to this space", {"space_id": space_id})

    def require_edit(self, m: Memory, allow_active: bool = False) -> None:
        """Agents may edit only their OWN memories that are still pending review (candidate)
        or active when auto_approve_agent_writes is enabled."""
        if not self.is_agent:
            return
        assert self.actor is not None
        self.require_write_space(m.scope_id)
        allowed = ("candidate", "active") if allow_active else ("candidate",)
        if m.created_by != self.actor.actor_id or m.status not in allowed:
            raise PermissionDeniedError(
                "Agents may only edit their own pending (candidate) memories",
                {"memory_id": m.memory_id},
            )


UNRESTRICTED = Access(None, None, None)


def resolve(uow: UnitOfWork, actor: Actor | None) -> Access:
    if actor is None or actor.actor_type is not ActorType.AGENT:
        return Access(None, None, actor)
    agent = uow.agents.get(agent_id_of(actor))
    if agent is None or not agent.enabled:
        raise AuthenticationRequiredError("Agent is unknown or disabled")
    shared = uow.spaces.shared_ids()
    rows = uow.spaces.active_grants(agent.agent_id)
    readable = shared | {r[0] for r in rows}
    writable = shared | {r[0] for r in rows if r[1] in WRITE_ROLES}
    return Access(frozenset(readable), frozenset(writable), actor)

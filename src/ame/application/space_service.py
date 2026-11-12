"""Space / Agent registry services (ADR-018, Phase 3a ADR-024/025).

- Only the user may create spaces, register agents, issue/rotate keys, enable/disable agents
  and grant/revoke space access (PHASE_3_REVIEW decisions 3 and 6).
- Every one of those writes an append-only audit event in the same transaction.
- Agent keys are returned exactly once; only ``sha256$<hex>`` of the key is stored.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from ame.application import audit
from ame.application.access import resolve
from ame.application.credentials import new_agent_key
from ame.application.ids import utc_now
from ame.application.schemas import (
    Actor,
    AgentCreate,
    AgentCredential,
    AgentRead,
    SpaceCreate,
    SpaceGrant,
    SpaceMemberRead,
    SpaceRead,
)
from ame.domain.enums import ActorType, AuditEntityType, AuditEventType
from ame.errors import (
    AlreadyExistsError,
    InvalidTransitionError,
    NotFoundError,
    TransitionNotPermittedError,
)
from ame.infrastructure.models import Agent, Space, SpaceMember
from ame.infrastructure.unit_of_work import UnitOfWork


def _require_user(actor: Actor, what: str) -> None:
    if actor.actor_type is not ActorType.USER:
        raise TransitionNotPermittedError(f"Only the user may {what}", {"actor": actor.actor_type})


def space_read(s: Space) -> SpaceRead:
    return SpaceRead.model_validate({k: getattr(s, k) for k in SpaceRead.model_fields})


def agent_read(a: Agent) -> AgentRead:
    return AgentRead(
        agent_id=a.agent_id,
        display_name=a.display_name,
        agent_type=a.agent_type,
        enabled=bool(a.enabled),
        has_credential=a.credential_hash is not None,
        created_at=a.created_at,
    )


def member_read(m: SpaceMember) -> SpaceMemberRead:
    return SpaceMemberRead.model_validate({k: getattr(m, k) for k in SpaceMemberRead.model_fields})


class SpaceService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def create_space(self, cmd: SpaceCreate, actor: Actor) -> SpaceRead:
        _require_user(actor, "create spaces")
        with UnitOfWork(self._sf) as uow:
            if uow.spaces.get(cmd.space_id) is not None:
                raise AlreadyExistsError("Space already exists", {"space_id": cmd.space_id})
            if cmd.owner_agent_id is not None and uow.agents.get(cmd.owner_agent_id) is None:
                raise NotFoundError("Agent not found", {"agent_id": cmd.owner_agent_id})
            s = Space(
                space_id=cmd.space_id,
                name=cmd.name,
                scope_type=cmd.scope_type.value,
                owner_agent_id=cmd.owner_agent_id,
                created_at=utc_now(),
            )
            uow.spaces.add(s)
            audit.record(
                uow, AuditEventType.SPACE_CREATED, AuditEntityType.SPACE, s.space_id, actor,
                "space created",
                after={"scope_type": s.scope_type, "owner_agent_id": s.owner_agent_id},
            )  # fmt: skip
            result = space_read(s)
            uow.commit()
            return result

    def get_space(self, space_id: str, actor: Actor | None = None) -> SpaceRead:
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            s = uow.spaces.get(space_id)
            if s is None or not acc.can_read(space_id):
                raise NotFoundError("Space not found", {"space_id": space_id})
            return space_read(s)

    def list_spaces(self, actor: Actor | None = None) -> list[SpaceRead]:
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            return [space_read(s) for s in uow.spaces.list_all() if acc.can_read(s.space_id)]

    def grant(self, space_id: str, cmd: SpaceGrant, actor: Actor) -> SpaceMemberRead:
        """Grant (or change the role of) an agent on a space. Re-granting a revoked member
        reactivates the same row; history stays in the audit log (rows are never deleted)."""
        _require_user(actor, "grant space access")
        with UnitOfWork(self._sf) as uow:
            if uow.spaces.get(space_id) is None:
                raise NotFoundError("Space not found", {"space_id": space_id})
            if uow.agents.get(cmd.agent_id) is None:
                raise NotFoundError("Agent not found", {"agent_id": cmd.agent_id})
            now = utc_now()
            m = uow.spaces.member(space_id, cmd.agent_id)
            before = None
            if m is None:
                m = SpaceMember(
                    space_id=space_id, agent_id=cmd.agent_id, role=cmd.role.value,
                    granted_at=now, granted_by=actor.actor_id,
                )  # fmt: skip
                uow.spaces.add_member(m)
            else:
                before = {"role": m.role, "revoked": m.revoked_at is not None}
                m.role, m.granted_at, m.granted_by = cmd.role.value, now, actor.actor_id
                m.revoked_at = m.revoked_by = None
                uow.memories.flush()
            audit.record(
                uow, AuditEventType.SPACE_MEMBER_GRANTED, AuditEntityType.SPACE, space_id, actor,
                cmd.reason, before=before, after={"agent_id": cmd.agent_id, "role": m.role},
            )  # fmt: skip
            result = member_read(m)
            uow.commit()
            return result

    def revoke(self, space_id: str, agent_id: str, reason: str, actor: Actor) -> SpaceMemberRead:
        _require_user(actor, "revoke space access")
        with UnitOfWork(self._sf) as uow:
            m = uow.spaces.member(space_id, agent_id)
            if m is None:
                raise NotFoundError(
                    "Membership not found", {"space_id": space_id, "agent_id": agent_id}
                )
            if m.revoked_at is not None:
                raise InvalidTransitionError("Membership already revoked", {"agent_id": agent_id})
            m.revoked_at, m.revoked_by = utc_now(), actor.actor_id
            uow.memories.flush()
            audit.record(
                uow, AuditEventType.SPACE_MEMBER_REVOKED, AuditEntityType.SPACE, space_id, actor,
                reason, before={"agent_id": agent_id, "role": m.role},
                after={"agent_id": agent_id, "revoked": True},
            )  # fmt: skip
            result = member_read(m)
            uow.commit()
            return result

    def list_members(self, space_id: str, actor: Actor) -> list[SpaceMemberRead]:
        _require_user(actor, "list space members")
        with UnitOfWork(self._sf, read_only=True) as uow:
            if uow.spaces.get(space_id) is None:
                raise NotFoundError("Space not found", {"space_id": space_id})
            return [member_read(m) for m in uow.spaces.members_of(space_id)]


class AgentService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def register_agent(self, cmd: AgentCreate, actor: Actor) -> AgentRead:
        """Register only; no key yet. Use issue_credential (user-only) to hand out a key."""
        _require_user(actor, "register agents")
        with UnitOfWork(self._sf) as uow:
            if uow.agents.get(cmd.agent_id) is not None:
                raise AlreadyExistsError("Agent already exists", {"agent_id": cmd.agent_id})
            a = Agent(
                agent_id=cmd.agent_id,
                display_name=cmd.display_name,
                agent_type=cmd.agent_type,
                credential_hash=None,
                enabled=1,
                created_at=utc_now(),
            )
            uow.agents.add(a)
            audit.record(
                uow, AuditEventType.AGENT_REGISTERED, AuditEntityType.AGENT, a.agent_id, actor,
                "agent registered", after={"agent_type": a.agent_type},
            )  # fmt: skip
            result = agent_read(a)
            uow.commit()
            return result

    def issue_credential(self, agent_id: str, actor: Actor, reason: str) -> AgentCredential:
        """Issue or rotate an agent key. The old key stops working immediately.
        The plaintext key is returned ONCE and never stored, logged or audited."""
        _require_user(actor, "issue agent keys")
        with UnitOfWork(self._sf) as uow:
            a = uow.agents.get(agent_id)
            if a is None:
                raise NotFoundError("Agent not found", {"agent_id": agent_id})
            key, digest = new_agent_key()
            rotated = a.credential_hash is not None
            a.credential_hash, a.credential_issued_at = digest, utc_now()
            uow.memories.flush()
            audit.record(
                uow, AuditEventType.AGENT_CREDENTIAL_ISSUED, AuditEntityType.AGENT, agent_id,
                actor, reason, after={"rotated": rotated},
            )  # fmt: skip
            issued_at = a.credential_issued_at
            uow.commit()
            return AgentCredential(agent_id=agent_id, api_key=key, issued_at=issued_at)

    def set_enabled(self, agent_id: str, enabled: bool, actor: Actor, reason: str) -> AgentRead:
        _require_user(actor, "enable or disable agents")
        with UnitOfWork(self._sf) as uow:
            a = uow.agents.get(agent_id)
            if a is None:
                raise NotFoundError("Agent not found", {"agent_id": agent_id})
            if bool(a.enabled) == enabled:
                raise InvalidTransitionError(
                    "Agent already in that state", {"enabled": bool(a.enabled)}
                )
            a.enabled = 1 if enabled else 0
            a.disabled_at = None if enabled else utc_now()
            uow.memories.flush()
            audit.record(
                uow,
                AuditEventType.AGENT_ENABLED if enabled else AuditEventType.AGENT_DISABLED,
                AuditEntityType.AGENT, agent_id, actor, reason,
                before={"enabled": not enabled}, after={"enabled": enabled},
            )  # fmt: skip
            result = agent_read(a)
            uow.commit()
            return result

    def list_agents(self, actor: Actor | None = None) -> list[AgentRead]:
        """User: all agents. Agent: only itself."""
        with UnitOfWork(self._sf, read_only=True) as uow:
            rows = uow.agents.list_all()
            if actor is not None and actor.actor_type is ActorType.AGENT:
                rows = [a for a in rows if a.agent_id == actor.actor_id]
            return [agent_read(a) for a in rows]

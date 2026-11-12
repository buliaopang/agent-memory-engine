"""Bearer-token -> Actor (ADR-024). The caller can never choose its own identity.

- the local user token (token_store) -> actor user:local
- an agent key (``ame_agt_...``) of an ENABLED agent -> actor agent <agent_id>
- anything else, or a disabled agent -> 401
Tokens are never logged.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from ame.application.access import agent_actor
from ame.application.credentials import digest_key, digest_matches, looks_like_agent_key
from ame.application.schemas import Actor
from ame.domain.enums import ActorType
from ame.errors import AuthenticationRequiredError
from ame.infrastructure.token_store import TokenProvider, token_matches
from ame.infrastructure.unit_of_work import UnitOfWork

LOCAL_USER = Actor(actor_type=ActorType.USER, actor_id="user:local")


class AuthService:
    def __init__(
        self, session_factory: sessionmaker[Session], user_token: str | TokenProvider
    ) -> None:
        """``user_token`` is a fixed string (tests) or a TokenProvider (server, Phase 4 L4:
        rotation applies without restart)."""
        self._sf = session_factory
        self._user_token = user_token

    def _current(self) -> str | None:
        t = self._user_token
        return t if isinstance(t, str) else t.get()

    def authenticate(self, presented: str | None) -> Actor:
        if not presented:
            raise AuthenticationRequiredError("Missing or invalid API token")
        current = self._current()
        if current is not None and token_matches(current, presented):
            return LOCAL_USER
        if looks_like_agent_key(presented):
            with UnitOfWork(self._sf, read_only=True) as uow:
                a = uow.agents.by_credential_hash(digest_key(presented))
                if a is not None and a.enabled and digest_matches(a.credential_hash, presented):
                    return agent_actor(a.agent_id)
        raise AuthenticationRequiredError("Missing or invalid API token")

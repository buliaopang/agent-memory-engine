"""Step 3: SpaceService / AgentService basic create + query (ADR-018). No ACL yet."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from ame.application.schemas import AgentCreate, SpaceCreate
from ame.application.space_service import AgentService, SpaceService
from ame.errors import (
    AlreadyExistsError,
    NotFoundError,
    SpaceNotFoundError,
    TransitionNotPermittedError,
)
from ame.infrastructure.unit_of_work import make_session_factory
from tests.conftest import AGENT, USER, make


@pytest.fixture()
def engine(settings):  # type: ignore[no-untyped-def]
    """Phase 3a: this module tests registration itself, so it uses a DB WITHOUT the
    pre-registered conftest agent (added below, nothing above changed)."""
    from ame.infrastructure.database import create_sqlite_engine, run_migrations

    run_migrations(settings)
    eng = create_sqlite_engine(settings)
    yield eng
    eng.dispose()


@pytest.fixture()
def spaces(engine: Engine) -> SpaceService:
    return SpaceService(make_session_factory(engine))


@pytest.fixture()
def agents(engine: Engine) -> AgentService:
    return AgentService(make_session_factory(engine))


def test_default_shared_space_exists(spaces: SpaceService) -> None:
    assert [s.space_id for s in spaces.list_spaces()] == ["shared"]
    assert spaces.get_space("shared").scope_type == "shared"


def test_create_and_use_space(spaces, svc) -> None:
    s = spaces.create_space(SpaceCreate(space_id="project:x", name="X", scope_type="project"), USER)
    assert s.scope_type == "project" and s.owner_agent_id is None
    m = svc.create_memory(make(scope_type="project", scope_id="project:x"), USER)
    assert m.scope_id == "project:x"
    with pytest.raises(AlreadyExistsError):
        spaces.create_space(SpaceCreate(space_id="project:x", name="X", scope_type="project"), USER)


def test_memory_requires_existing_matching_space(svc) -> None:
    with pytest.raises(SpaceNotFoundError):
        svc.create_memory(make(scope_type="private", scope_id="private:none"), USER)
    with pytest.raises(SpaceNotFoundError):
        svc.create_memory(make(scope_type="private", scope_id="shared"), USER)


def test_only_user_creates_spaces_and_agents(spaces, agents) -> None:
    with pytest.raises(TransitionNotPermittedError):
        spaces.create_space(SpaceCreate(space_id="s", name="s", scope_type="shared"), AGENT)
    with pytest.raises(TransitionNotPermittedError):
        agents.register_agent(AgentCreate(agent_id="a", display_name="a", agent_type="x"), AGENT)


def test_agent_register_without_credential(agents, spaces) -> None:
    a = agents.register_agent(
        AgentCreate(agent_id="agent:codex", display_name="Codex", agent_type="codex"), USER
    )
    assert a.enabled and not a.has_credential
    assert [x.agent_id for x in agents.list_agents()] == ["agent:codex"]
    with pytest.raises(AlreadyExistsError):
        agents.register_agent(
            AgentCreate(agent_id="agent:codex", display_name="C", agent_type="codex"), USER
        )
    s = spaces.create_space(
        SpaceCreate(space_id="private:codex", name="p", scope_type="private",
                    owner_agent_id="agent:codex"), USER)  # fmt: skip
    assert s.owner_agent_id == "agent:codex"
    with pytest.raises(NotFoundError):
        spaces.create_space(
            SpaceCreate(space_id="p2", name="p", scope_type="private", owner_agent_id="nope"),
            USER,
        )


def test_space_id_validation() -> None:
    with pytest.raises(ValueError):
        SpaceCreate(space_id="bad id with spaces", name="x", scope_type="shared")

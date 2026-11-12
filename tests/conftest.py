from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ame.adapters.rest.app import create_app
from ame.application.memory_service import MemoryService
from ame.application.schemas import Actor, MemoryCreate
from ame.config import Settings
from ame.domain.enums import ActorType
from ame.infrastructure.database import create_sqlite_engine, run_migrations
from ame.infrastructure.unit_of_work import make_session_factory

USER = Actor(actor_type=ActorType.USER, actor_id="user:owner")
AGENT = Actor(actor_type=ActorType.AGENT, actor_id="agent:codex")
SYSTEM = Actor(actor_type=ActorType.SYSTEM, actor_id="system")


def register_test_agent(eng: Engine, agent_id: str = AGENT.actor_id) -> None:
    from sqlalchemy import text

    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agents (agent_id, display_name, agent_type, credential_hash, "
                "enabled, created_at) VALUES (:a, 'codex', 'test', NULL, 1, "
                "'2026-01-01T00:00:00Z')"
            ),
            {"a": agent_id},
        )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    # Phase 2: background index worker off in tests for determinism (tests drive it directly)
    return Settings(data_dir=tmp_path / "data", index_worker_enabled=False)


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture()
def engine(settings: Settings) -> Iterator[Engine]:
    run_migrations(settings)
    eng = create_sqlite_engine(settings)
    # Phase 3a (ADR-025): access is resolved from the agent registry, so the shared test
    # AGENT must exist and be enabled. Inserted as plain fixture data (no audit row) so the
    # pre-3a audit-sequence tests keep their exact expectations.
    register_test_agent(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def svc(engine: Engine) -> MemoryService:
    return MemoryService(make_session_factory(engine))


def make(**over: object) -> MemoryCreate:
    data: dict[str, object] = {
        "content": "c",
        "memory_type": "semantic",
        "layer": "L2",
        "scope_type": "shared",
        "scope_id": "shared",
        "importance": 0.5,
        "confidence": 0.5,
    }
    data.update(over)
    return MemoryCreate.model_validate(data)


def read_user_token(data_dir: Path) -> str:
    """Read the local user token the way the server stores it on this OS (Windows: DPAPI blob,
    no plaintext file - ADR-026). Tests must not read ``api_token`` directly."""
    from ame.infrastructure.token_store import default_protector, load_or_create_token

    return load_or_create_token(data_dir, default_protector())

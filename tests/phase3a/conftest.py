"""Phase 3a fixtures: a REST app with the user token plus helpers to mint agent clients."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ame.adapters.rest.app import create_app
from ame.config import Settings
from tests.conftest import read_user_token

B = "/api/v1"


def mem(scope_id: str = "shared", scope_type: str = "shared", **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "content": "内容 content",
        "memory_type": "semantic",
        "layer": "L2",
        "scope_type": scope_type,
        "scope_id": scope_id,
        "importance": 0.5,
        "confidence": 0.5,
    }
    d.update(over)
    return d


@pytest.fixture()
def app_user(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = f"Bearer {read_user_token(settings.data_dir)}"
        yield c


@pytest.fixture()
def make_agent(app_user: TestClient) -> Callable[[str], TestClient]:
    """Register an agent as the user, issue its key, return a client that uses that key."""
    made: list[TestClient] = []

    def _mk(agent_id: str) -> TestClient:
        r = app_user.post(
            f"{B}/agents", json={"agent_id": agent_id, "display_name": agent_id, "agent_type": "t"}
        )
        assert r.status_code == 201, r.text
        k = app_user.post(f"{B}/agents/{agent_id}/credentials", json={"reason": "test"})
        assert k.status_code == 201, k.text
        c = TestClient(app_user.app)
        c.headers["Authorization"] = f"Bearer {k.json()['api_key']}"
        made.append(c)
        return c

    return _mk


def space(user: TestClient, space_id: str, scope_type: str) -> None:
    r = user.post(f"{B}/spaces", json={"space_id": space_id, "name": space_id,
                                       "scope_type": scope_type})  # fmt: skip
    assert r.status_code == 201, r.text


def grant(user: TestClient, space_id: str, agent_id: str, role: str) -> None:
    r = user.post(f"{B}/spaces/{space_id}/members",
                  json={"agent_id": agent_id, "role": role, "reason": "test"})  # fmt: skip
    assert r.status_code == 200, r.text

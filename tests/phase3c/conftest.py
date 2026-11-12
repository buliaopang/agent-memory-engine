"""Phase 3c fixtures: REST app with MCP switched on (read-write unless a test says otherwise),
and an MCP-adapter ``AmeTools`` wired to that app through a TestClient transport."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ame.adapters.mcp.client import RawResponse, RestClient, Transport
from ame.adapters.mcp.tools import AmeTools
from ame.config import Settings
from tests.phase3a.conftest import app_user, make_agent

__all__ = ["app_user", "make_agent"]
B = "/api/v1"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", index_worker_enabled=False,
                    mcp_enabled=True, mcp_read_only=False)  # fmt: skip


def testclient_transport(c: TestClient, log: list[dict[str, Any]] | None = None) -> Transport:
    def send(method: str, path: str, headers: dict[str, str], body: bytes | None) -> RawResponse:
        if log is not None:
            log.append({"method": method, "path": path, "headers": dict(headers)})
        r = c.request(method, path, headers=headers, content=body)
        return RawResponse(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)

    return send


def agent_key(app_user: TestClient, agent_id: str) -> str:
    r = app_user.post(f"{B}/agents", json={"agent_id": agent_id, "display_name": agent_id,
                                           "agent_type": "t"})  # fmt: skip
    assert r.status_code == 201, r.text
    k = app_user.post(f"{B}/agents/{agent_id}/credentials", json={"reason": "test"})
    assert k.status_code == 201, k.text
    key: str = k.json()["api_key"]
    return key


@pytest.fixture()
def mcp_tools(app_user: TestClient) -> Callable[..., AmeTools]:
    def _mk(agent_id: str, log: list[dict[str, Any]] | None = None) -> AmeTools:
        key = agent_key(app_user, agent_id)
        c = TestClient(app_user.app)
        return AmeTools(RestClient(testclient_transport(c, log), key, sleep=lambda _s: None))

    return _mk

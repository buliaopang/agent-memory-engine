"""Phase 4 fixtures: reuse the Phase 3a REST helpers (user client, agent clients, spaces)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.phase3a.conftest import B, app_user, grant, make_agent, mem, space

__all__ = ["B", "app_user", "grant", "make_agent", "mem", "space", "write"]


def write(c: TestClient, **over: Any) -> str:
    r = c.post(f"{B}/memories", json=mem(**over))
    assert r.status_code == 201, r.text
    return str(r.json()["memory_id"])

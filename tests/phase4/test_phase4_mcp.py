"""Phase 4 MCP tools through the adapter (no SDK needed): feedback, spine, epochs."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ame.config import Settings
from tests.phase3a.conftest import app_user
from tests.phase4.conftest import B, write

__all__ = ["app_user"]


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", index_worker_enabled=False,
                    mcp_enabled=True, mcp_read_only=False)  # fmt: skip


def test_mcp_feedback_spine_epochs(app_user: TestClient, settings: Settings) -> None:
    from ame.adapters.mcp.client import RestClient
    from ame.adapters.mcp.tools import AmeTools
    from tests.phase3c.conftest import agent_key, testclient_transport
    from tests.phase4.conftest import grant

    mid = write(app_user, content="mcp 记忆")
    app_user.post(f"{B}/epochs", json={"name": "季度", "reason": "r"})
    key = agent_key(app_user, "agent:mcp4")
    grant(app_user, "shared", "agent:mcp4", "reader")
    log: list[dict[str, object]] = []
    t = AmeTools(RestClient(testclient_transport(TestClient(app_user.app), log), key,
                            sleep=lambda _s: None))  # fmt: skip
    r = t.memory_feedback(mid, "useful", "有帮助")
    assert r["ok"], r
    assert any("idempotency-key" in {k.lower() for k in e["headers"]} for e in log)  # type: ignore[attr-defined]
    assert t.memory_spine()["ok"] and t.memory_list_epochs()["ok"]
    assert t.memory_feedback("mem_nope", "useful", "x")["ok"] is False
    assert t.memory_feedback(mid, "bogus", "x")["ok"] is False


def test_feedback_contract_matches_dto() -> None:
    import json

    from ame.application.schemas import FeedbackCreate

    root = Path(__file__).resolve().parents[2]
    c = json.loads((root / "schemas" / "mcp_tools.json").read_text(encoding="utf-8"))
    names = set(c["tools"]["memory_feedback"]["input"])
    assert set(FeedbackCreate.model_fields) | {"memory_id", "idempotency_key"} == names


def test_read_only_mode_blocks_feedback_over_rest(settings: Settings) -> None:
    from ame.adapters.mcp.client import RestClient
    from ame.adapters.mcp.tools import AmeTools
    from ame.adapters.rest.app import create_app
    from tests.conftest import read_user_token
    from tests.phase3c.conftest import agent_key, testclient_transport
    from tests.phase4.conftest import grant

    ro = settings.model_copy(update={"mcp_read_only": True})
    with TestClient(create_app(ro)) as u:
        u.headers["Authorization"] = f"Bearer {read_user_token(ro.data_dir)}"
        mid = write(u)
        key = agent_key(u, "agent:ro4")
        grant(u, "shared", "agent:ro4", "editor")
        t = AmeTools(RestClient(testclient_transport(TestClient(u.app)), key,
                                sleep=lambda _s: None))  # fmt: skip
        r = t.memory_feedback(mid, "useful", "x")
        assert r["ok"] is False
        assert t.memory_spine()["ok"] and t.memory_list_epochs()["ok"]

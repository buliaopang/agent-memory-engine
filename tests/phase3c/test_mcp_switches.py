"""ADR-028 switches: server-side backstop on the ame-mcp channel, log-only origin marking, key
never logged, ame-mcp refuses to start when disabled, no DB access from the adapter."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ame.adapters.mcp import server as mcp_server
from ame.adapters.mcp.client import USER_AGENT
from ame.adapters.rest.app import create_app
from ame.config import Settings
from tests.conftest import read_user_token
from tests.phase3c.conftest import agent_key

B = "/api/v1"
BODY = {"content": "c", "memory_type": "semantic", "layer": "L2", "scope_type": "shared",
        "scope_id": "shared", "importance": 0.5, "confidence": 0.5}  # fmt: skip


def _app(tmp_path: Path, **kw: bool) -> TestClient:
    s = Settings(data_dir=tmp_path / "d", index_worker_enabled=False, **kw)  # type: ignore[arg-type]
    c = TestClient(create_app(s))
    c.__enter__()
    c.headers["Authorization"] = f"Bearer {read_user_token(s.data_dir)}"
    return c


def test_default_settings_mcp_off_and_read_only() -> None:
    s = Settings()
    assert s.mcp_enabled is False and s.mcp_read_only is True and s.mcp_max_content_chars == 4000


def test_disabled_blocks_mcp_channel_but_not_rest(tmp_path: Path) -> None:
    c = _app(tmp_path)
    key = agent_key(c, "agent:a")
    h = {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
    r = c.get(f"{B}/stats", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "MCP_DISABLED"
    assert c.get(f"{B}/stats", headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_read_only_allows_reads_blocks_writes(tmp_path: Path) -> None:
    c = _app(tmp_path, mcp_enabled=True, mcp_read_only=True)
    key = agent_key(c, "agent:a")
    h = {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
    assert c.get(f"{B}/stats", headers=h).status_code == 200
    assert c.post(f"{B}/memories/search", json={"query": "x"}, headers=h).status_code == 200
    assert c.post(f"{B}/context", json={"task_description": "x"}, headers=h).status_code == 200
    w = c.post(f"{B}/memories", json=BODY, headers=h)
    assert w.status_code == 403 and w.json()["error"]["code"] == "MCP_DISABLED"
    assert c.get(f"{B}/stats").json()["memories"]["total"] == 0


def test_read_write_allows_writes(tmp_path: Path) -> None:
    c = _app(tmp_path, mcp_enabled=True, mcp_read_only=False)
    key = agent_key(c, "agent:a")
    h = {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
    assert c.post(f"{B}/memories", json=BODY, headers=h).status_code == 201


def test_origin_logged_as_mcp_without_key_or_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    c = _app(tmp_path, mcp_enabled=True, mcp_read_only=False)
    key = agent_key(c, "agent:a")
    capsys.readouterr()
    body = dict(BODY, content="SECRET-CONTENT-123")
    c.post(f"{B}/memories", json=body,
           headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT})  # fmt: skip
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "client" in logs and "mcp" in logs
    assert key not in logs and "SECRET-CONTENT-123" not in logs


def test_ame_mcp_refuses_when_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AME_MCP_ENABLED", raising=False)
    assert mcp_server.main(["--agent", "agent:a"]) == 3
    assert "disabled" in capsys.readouterr().err


def test_ame_mcp_no_key_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("mcp")
    monkeypatch.setenv("AME_MCP_ENABLED", "true")
    monkeypatch.setenv("AME_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AME_AGENT_KEY", raising=False)
    assert mcp_server.main(["--agent", "agent:a"]) == 3
    err = capsys.readouterr()
    assert "ame mcp setup" in err.err and err.out == ""


def test_adapter_never_touches_database() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "ame" / "adapters" / "mcp"
    banned = ("sqlalchemy", "sqlite3", "ame.infrastructure.database",
              "ame.infrastructure.repositories", "ame.infrastructure.unit_of_work",
              "ame.application")  # fmt: skip
    for f in root.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom | ast.Import):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [
                    a.name for a in node.names]  # fmt: skip
                for n in names:
                    assert not n.startswith(banned), f"{f.name} imports {n}"


def test_contract_has_16_tools_15_implemented_feedback_deferred() -> None:
    from ame.adapters.mcp.tools import ALL_TOOLS, DEFERRED_TOOLS, READ_TOOLS

    root = Path(__file__).resolve().parents[2]
    contract = json.loads((root / "schemas" / "mcp_tools.json").read_text(encoding="utf-8"))
    names = set(contract["tools"])
    impl = {t.name for t in ALL_TOOLS}
    # Phase 4 (approved plan, L6): memory_feedback implemented, +memory_spine/list_epochs.
    assert len(names) == 18 and len(impl) == 18 and len(READ_TOOLS) == 8
    assert names - impl == set(DEFERRED_TOOLS) == set()
    assert names == impl

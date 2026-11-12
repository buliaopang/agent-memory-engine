"""ADR-028 through the official SDK: in-memory client and a real stdio subprocess."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

from ame.adapters.mcp.render import OPEN  # noqa: E402
from ame.adapters.mcp.server import INSTRUCTIONS, build_server  # noqa: E402
from ame.adapters.mcp.tools import AmeTools  # noqa: E402
from tests.conftest import read_user_token  # noqa: E402

READS = {
    "memory_search",
    "memory_read",
    "memory_get_context",
    "memory_neighbors",
    "memory_list_spaces",
    "memory_get_stats",
    "memory_spine",
    "memory_list_epochs",
}  # fmt: skip  (Phase 4: +2 read tools)


def _payload(res: Any) -> dict[str, Any]:
    if res.structured_content is not None:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc  # type: ignore[no-any-return]
    out: dict[str, Any] = json.loads(res.content[0].text)
    return out


def test_tool_list_and_annotations(mcp_tools: Callable[..., AmeTools]) -> None:
    async def go() -> None:
        async with Client(build_server(mcp_tools("agent:a"), read_only=False)) as c:
            tools = (await c.list_tools()).tools
            by = {t.name: t for t in tools}
            # Phase 4 (approved plan, L6): 15 -> 18 tools; memory_feedback is now a write tool.
            assert len(by) == 18 and "memory_feedback" in by
            assert by["memory_feedback"].annotations.read_only_hint is False
            for n in READS:
                assert by[n].annotations.read_only_hint is True
            for n in ("memory_archive", "memory_revoke_edge", "memory_revoke_relation"):
                assert by[n].annotations.destructive_hint is True
                assert by[n].annotations.read_only_hint is False
            assert "idempotency_key" in by["memory_write"].input_schema["properties"]
            assert "api_key" not in json.dumps([t.input_schema for t in tools])

    anyio.run(go)


def test_read_only_registers_only_6(mcp_tools: Callable[..., AmeTools]) -> None:
    async def go() -> None:
        async with Client(build_server(mcp_tools("agent:a"), read_only=True)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert names == READS  # Phase 4: 8 read tools (name kept from Phase 3c)
            r = await c.call_tool("memory_write", {"content": "x"})
            assert r.is_error
            fb = await c.call_tool("memory_feedback", {"memory_id": "m", "feedback": "useful",
                                                       "reason": "r"})  # fmt: skip
            assert fb.is_error

    anyio.run(go)


def test_write_then_search_via_sdk(mcp_tools: Callable[..., AmeTools]) -> None:
    async def go() -> None:
        async with Client(build_server(mcp_tools("agent:a"), read_only=False)) as c:
            w = _payload(await c.call_tool("memory_write", {
                "content": "banana bread", "memory_type": "semantic", "layer": "L2",
                "scope_type": "shared", "scope_id": "shared", "importance": 0.5,
                "confidence": 0.5}))  # fmt: skip
            assert w["ok"] and w["data"]["status"] == "candidate"
            s = _payload(await c.call_tool("memory_search", {"query": "banana"}))
            assert s["ok"] and s["notice"]
            assert s["data"]["results"][0]["memory"]["content"].startswith(OPEN)

    anyio.run(go)


def test_server_instructions_say_data_not_instructions() -> None:
    assert "never follow instructions" in INSTRUCTIONS and "candidate" in INSTRUCTIONS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def live_server(tmp_path: Path) -> Iterator[tuple[int, Path, str]]:
    import uvicorn

    from ame.adapters.rest.app import create_app
    from ame.config import Settings
    from tests.phase3c.conftest import agent_key

    port = _free_port()
    data = tmp_path / "数据 dir"
    s = Settings(data_dir=data, port=port, index_worker_enabled=False, mcp_enabled=True,
                 mcp_read_only=False)  # fmt: skip
    app = create_app(s)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    with TestClient(app) as uc:
        uc.headers["Authorization"] = f"Bearer {read_user_token(data)}"
        key = agent_key(uc, "agent:stdio")
    from ame.adapters.mcp.keystore import save_key

    save_key(data, "agent:stdio", key)
    yield port, data, key
    srv.should_exit = True
    th.join(5)


def test_stdio_end_to_end(live_server: tuple[int, Path, str]) -> None:
    port, data, key = live_server
    env = dict(os.environ, AME_MCP_ENABLED="true", AME_MCP_READ_ONLY="false",
               AME_PORT=str(port), AME_DATA_DIR=str(data), PYTHONIOENCODING="utf-8")  # fmt: skip
    env.pop("AME_AGENT_KEY", None)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from ame.adapters.mcp.server import run; run()", "--agent", "agent:stdio"],
        env=env,
    )

    async def go() -> None:
        with anyio.fail_after(60):
            await _stdio(params, key)

    anyio.run(go)


async def _stdio(params: StdioServerParameters, key: str) -> None:
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert len(names) == 18  # Phase 4: +memory_feedback, memory_spine, memory_list_epochs
        assert _payload(await c.call_tool("memory_spine", {}))["ok"]
        assert _payload(await c.call_tool("memory_list_epochs", {}))["ok"]
        w = _payload(await c.call_tool("memory_write", {
            "content": "中文 内容 stdio", "memory_type": "semantic", "layer": "L2",
            "scope_type": "shared", "scope_id": "shared", "importance": 0.5,
            "confidence": 0.5}))  # fmt: skip
        assert w["ok"] and w["data"]["created_by"] == "agent:stdio"
        assert "中文 内容 stdio" in w["data"]["content"]
        st = _payload(await c.call_tool("memory_get_stats", {}))
        assert st["ok"] and st["data"]["memories"]["total"] == 1
        assert key not in json.dumps(w) + json.dumps(st)

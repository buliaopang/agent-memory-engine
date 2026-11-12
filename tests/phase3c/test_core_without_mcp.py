"""ADR-028: the core install must work with the optional ``mcp`` SDK absent. A subprocess blocks
``import mcp`` and exercises the REST app, the CLI and ame-mcp's friendly failure."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("blocked for test: " + name)
        return None
sys.meta_path.insert(0, Block())
from pathlib import Path
from fastapi.testclient import TestClient
from ame.adapters.rest.app import create_app
from ame.config import Settings
from ame.infrastructure.token_store import default_protector, load_or_create_token
from ame.cli import main
import ame.adapters.mcp.tools, ame.adapters.mcp.client, ame.adapters.mcp.render
d = Path(sys.argv[1])
s = Settings(data_dir=d, index_worker_enabled=False)
with TestClient(create_app(s)) as c:
    c.headers["Authorization"] = "Bearer " + load_or_create_token(d, default_protector())
    assert c.get("/api/v1/stats").status_code == 200
    assert c.post("/api/v1/context", json={"task_description": "x"}).status_code == 200
assert main(["check-graph", "--db", str(s.db_path)]) == 0
import os
os.environ["AME_MCP_ENABLED"] = "true"
from ame.adapters.mcp.server import main as mcp_main
assert mcp_main(["--agent", "agent:a"]) == 3
assert "mcp" not in sys.modules
print("CORE-OK")
"""


def test_core_works_without_mcp_sdk(tmp_path: Path) -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("AME_MCP_ENABLED", None)
    r = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path)], capture_output=True,
                       text=True, env=env, timeout=120)  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    assert "CORE-OK" in r.stdout
    assert "pip install ame[mcp]" in r.stderr

"""Per-agent MCP key storage (ADR-028).

- POSIX: ``<data_dir>/mcp/<agent>.key`` with mode 0600.
- Windows: DPAPI blob ``<data_dir>/mcp/<agent>.key.dpapi`` (CurrentUser). **UNVERIFIED on a real
  Windows machine** (WINDOWS_验收指南 W28).
Lookup order for the server: env ``AME_AGENT_KEY`` first, then the stored file.
The key is never printed by these helpers and never logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ame.infrastructure.token_store import TokenProtector, _write_new, default_protector

ENV_KEY = "AME_AGENT_KEY"
KEY_PREFIX = "ame_agt_"
_AGENT_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,128}$")


class KeyNotFoundError(Exception):
    """No key in env and no stored key file for this agent."""


def _safe_name(agent_id: str) -> str:
    if not _AGENT_RE.match(agent_id) or ".." in agent_id:
        raise ValueError("invalid agent id")
    return agent_id.replace(":", "_")


def key_path(data_dir: Path, agent_id: str, protected: bool) -> Path:
    suffix = ".key.dpapi" if protected else ".key"
    return data_dir / "mcp" / f"{_safe_name(agent_id)}{suffix}"


def save_key(
    data_dir: Path, agent_id: str, key: str, protector: TokenProtector | None = None
) -> Path:
    if not key.startswith(KEY_PREFIX):
        raise ValueError("not an agent key")
    (data_dir / "mcp").mkdir(parents=True, exist_ok=True)
    if protector is None:
        p = key_path(data_dir, agent_id, protected=False)
        _write_new(p, key.encode("utf-8"))
        return p
    blob = protector.protect(key.encode("utf-8"))
    if protector.unprotect(blob).decode("utf-8") != key:
        raise OSError("key protector round-trip failed")
    p = key_path(data_dir, agent_id, protected=True)
    _write_new(p, blob)
    plain = key_path(data_dir, agent_id, protected=False)
    if plain.exists():
        plain.unlink()
    return p


def load_key(
    data_dir: Path,
    agent_id: str,
    protector: TokenProtector | None = None,
    env: dict[str, str] | None = None,
) -> str:
    environ = os.environ if env is None else env
    from_env = environ.get(ENV_KEY, "").strip()
    if from_env:
        return from_env
    if protector is not None:
        p = key_path(data_dir, agent_id, protected=True)
        if p.exists():
            return protector.unprotect(p.read_bytes()).decode("utf-8").strip()
    p = key_path(data_dir, agent_id, protected=False)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    raise KeyNotFoundError(
        f"no key for {agent_id}: run `ame mcp setup --agent {agent_id}` or set {ENV_KEY}"
    )


def platform_protector() -> TokenProtector | None:
    return default_protector()

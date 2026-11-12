"""ADR-028 key storage + `ame mcp setup` (snippets carry no key)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ame.adapters.mcp.keystore import (
    ENV_KEY,
    KeyNotFoundError,
    key_path,
    load_key,
    platform_protector,
    save_key,
)
from ame.cli import main
from ame.config import Settings
from ame.infrastructure.database import run_migrations

KEY = "ame_agt_" + "x" * 40


class FakeProtector:
    def protect(self, data: bytes) -> bytes:
        return b"ENC" + bytes(b ^ 0x5A for b in data)

    def unprotect(self, blob: bytes) -> bytes:
        assert blob.startswith(b"ENC")
        return bytes(b ^ 0x5A for b in blob[3:])


def test_posix_file_0600_and_roundtrip(tmp_path: Path) -> None:
    p = save_key(tmp_path, "agent:codex", KEY)
    assert p.name == "agent_codex.key"
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert load_key(tmp_path, "agent:codex", env={}) == KEY


def test_protected_blob_has_no_plaintext(tmp_path: Path) -> None:
    save_key(tmp_path, "agent:a", KEY)  # an old plaintext copy is removed
    p = save_key(tmp_path, "agent:a", KEY, FakeProtector())
    assert p.name.endswith(".key.dpapi") and KEY.encode() not in p.read_bytes()
    assert not key_path(tmp_path, "agent:a", protected=False).exists()
    assert load_key(tmp_path, "agent:a", FakeProtector(), env={}) == KEY


def test_env_takes_precedence_and_missing_raises(tmp_path: Path) -> None:
    assert load_key(tmp_path, "agent:a", env={ENV_KEY: "ame_agt_env"}) == "ame_agt_env"
    with pytest.raises(KeyNotFoundError):
        load_key(tmp_path, "agent:a", env={})


def test_bad_agent_id_and_non_agent_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        save_key(tmp_path, "../evil", KEY)
    with pytest.raises(ValueError):
        save_key(tmp_path, "agent:a", "user-token")


def test_cli_setup_stores_key_and_prints_no_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = Settings(data_dir=tmp_path)
    run_migrations(s)
    db = str(s.db_path)
    assert main(["agent", "--db", db, "create", "agent:codex", "--agent-type", "codex",
                 "--no-key"]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["mcp", "setup", "--agent", "agent:codex", "--db", db]) == 0
    out = capsys.readouterr().out
    stored = load_key(tmp_path, "agent:codex", platform_protector(), env={})
    assert stored.startswith("ame_agt_") and stored not in out
    assert "[mcp_servers.ame]" in out and "mcpServers" in out and "UNVERIFIED" in out

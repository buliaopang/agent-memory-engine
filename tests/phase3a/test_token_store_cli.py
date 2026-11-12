"""Phase 3a user token: protector (DPAPI stand-in), rotation, and the agent/space CLI."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from ame.cli import main
from ame.config import Settings
from ame.infrastructure.database import run_migrations
from ame.infrastructure.token_store import (
    dpapi_path,
    load_or_create_token,
    rotate_token,
    token_path,
)


class FakeProtector:
    """XOR stand-in for DPAPI: proves the plaintext never lands on disk."""

    def protect(self, data: bytes) -> bytes:
        return b"P1" + bytes(b ^ 0x5A for b in data)

    def unprotect(self, blob: bytes) -> bytes:
        assert blob[:2] == b"P1"
        return bytes(b ^ 0x5A for b in blob[2:])


def test_protected_token_never_plaintext(tmp_path: Path) -> None:
    p = FakeProtector()
    tok = load_or_create_token(tmp_path, p)
    assert not token_path(tmp_path).exists()
    assert tok.encode() not in dpapi_path(tmp_path).read_bytes()
    assert load_or_create_token(tmp_path, p) == tok


def test_plaintext_token_is_migrated_with_same_value(tmp_path: Path) -> None:
    tok = load_or_create_token(tmp_path)  # pre-3a style file
    assert token_path(tmp_path).exists()
    assert load_or_create_token(tmp_path, FakeProtector()) == tok
    assert not token_path(tmp_path).exists() and dpapi_path(tmp_path).exists()


@pytest.mark.parametrize("protected", [False, True])
def test_rotate_changes_token(tmp_path: Path, protected: bool) -> None:
    p = FakeProtector() if protected else None
    t1 = load_or_create_token(tmp_path, p)
    t2 = rotate_token(tmp_path, p)
    assert t1 != t2 and load_or_create_token(tmp_path, p) == t2
    if not protected and os.name == "posix":
        assert stat.S_IMODE(token_path(tmp_path).stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path")
def test_posix_default_has_no_protector() -> None:
    from ame.infrastructure.token_store import default_protector

    assert default_protector() is None


def test_cli_agent_space_token(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    data = tmp_path / "data"
    monkeypatch.setenv("AME_DATA_DIR", str(data))
    s = Settings(data_dir=data)
    run_migrations(s)
    db = str(s.db_path)
    assert main(["agent", "--db", db, "create", "agent:cli", "--agent-type", "t"]) == 0
    out = capsys.readouterr().out
    key = out.strip().splitlines()[-1]
    assert key.startswith("ame_agt_")
    assert main(["space", "--db", db, "create", "project:c", "--scope-type", "project"]) == 0
    assert main(["space", "--db", db, "grant", "project:c", "agent:cli", "--role", "reader",
                 "--reason", "r"]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["space", "--db", db, "members", "project:c"]) == 0
    assert "agent:cli\treader\tactive" in capsys.readouterr().out
    assert main(["space", "--db", db, "revoke", "project:c", "agent:cli", "--reason", "r"]) == 0
    assert main(["agent", "--db", db, "rotate-key", "agent:cli", "--reason", "r"]) == 0
    assert main(["agent", "--db", db, "disable", "agent:cli", "--reason", "r"]) == 0
    capsys.readouterr()
    assert main(["agent", "--db", db, "list"]) == 0
    assert "agent:cli\tdisabled\tkey" in capsys.readouterr().out
    assert main(["token", "show"]) == 0
    t1 = capsys.readouterr().out.strip()
    assert main(["token", "rotate"]) == 0
    rot = capsys.readouterr().out
    assert t1 not in rot  # rotate never prints the token
    assert main(["token", "show"]) == 0
    assert capsys.readouterr().out.strip() != t1
    assert main(["check-graph", "--db", db]) == 0

"""Phase 3b migration 0011 (idempotency_keys) on a populated DB: up/down/up, failure restore."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command

from ame.application.integrity_service import IntegrityService
from ame.errors import MigrationFailedError
from ame.infrastructure.migration_runner import MigrationRunner
from ame.infrastructure.schema_guards import HEAD, REVISIONS
from tests.phase3a.test_migrations_3a import _audit_fp, _cfg, _counts, _populated_0008

ROOT = Path(__file__).resolve().parents[2]


def _populated_0010(db: Path) -> None:
    _populated_0008(db)
    MigrationRunner(db).upgrade("0010_space_acl")


def _rev(db: Path) -> str:
    c = sqlite3.connect(db)
    try:
        return str(c.execute("SELECT version_num FROM alembic_version").fetchone()[0])
    finally:
        c.close()


def _tables(db: Path) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def test_head_is_0011() -> None:
    # Phase 4 (approved plan): HEAD moved on to 0015; 0011 must stay in the chain right
    # after 0010 and directly before 0012.
    assert HEAD == REVISIONS[-1] == "0016_single_parent"
    i = REVISIONS.index("0011_idempotency")
    assert REVISIONS[i - 1] == "0010_space_acl"
    assert REVISIONS[i + 1] == "0012_agent_key_index"


def test_upgrade_downgrade_upgrade_populated(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _populated_0010(db)
    fp0, counts0 = _audit_fp(db), _counts(db)
    rep = MigrationRunner(db).upgrade()
    assert rep.to_revision == HEAD and rep.backup is not None and not rep.restored
    assert "idempotency_keys" in _tables(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO idempotency_keys VALUES ('user:local', 'k', 'h', 'done', 201, '{}', "
              "'2026-11-03T00:00:00Z', '2026-11-04T00:00:00Z')")  # fmt: skip
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):  # done rows must carry a stored response
        c.execute("INSERT INTO idempotency_keys VALUES ('user:local', 'k2', 'h', 'done', NULL, "
                  "NULL, 't', 't')")  # fmt: skip
    c.close()
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert IntegrityService(db).run().exit_code == 0
    command.downgrade(_cfg(db), "0010_space_acl")
    assert _rev(db) == "0010_space_acl" and "idempotency_keys" not in _tables(db)
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert IntegrityService(db).run().exit_code == 0
    MigrationRunner(db).upgrade()
    assert _rev(db) == HEAD and "idempotency_keys" in _tables(db)
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert IntegrityService(db).run().exit_code == 0


def test_injected_failure_restores_backup(tmp_path: Path) -> None:
    """A stray table named idempotency_keys makes 0011's CREATE TABLE fail; the runner must
    restore the automatic backup and leave the DB at 0010 with every row intact."""
    db = tmp_path / "ame.db"
    _populated_0010(db)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE idempotency_keys (x INTEGER)")
    c.commit()
    c.close()
    fp0, counts0 = _audit_fp(db), _counts(db)
    with pytest.raises(MigrationFailedError) as ei:
        MigrationRunner(db).upgrade()
    assert "restored" in ei.value.message
    assert _rev(db) == "0010_space_acl"
    assert _audit_fp(db) == fp0 and _counts(db) == counts0


def test_executed_migration_hashes_unchanged() -> None:
    lines = (ROOT / "tests/phase12/executed_migrations.sha256").read_text().splitlines()
    seen = set()
    for line in lines:
        digest, name = line.split()
        assert hashlib.sha256((ROOT / "migrations/versions" / name).read_bytes()).hexdigest() == (
            digest
        ), name
        seen.add(name[:4])
    assert {f"{i:04d}" for i in range(1, 12)} <= seen

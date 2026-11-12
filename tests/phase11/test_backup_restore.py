"""E: ame backup / ame restore (ADR-014) - WAL-safe, never overwrite, pre-restore copy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ame.cli import main
from ame.errors import StorageError
from ame.infrastructure.backup import backup_database, inspect, restore_database
from ame.infrastructure.migration_runner import MigrationRunner
from tests.phase11.dbutil import add_memory


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "ame.db"
    MigrationRunner(p).upgrade()
    return p


def test_e1_backup_contains_uncheckpointed_wal_data(db, tmp_path):
    holder = sqlite3.connect(str(db), isolation_level=None)  # keeps WAL from checkpointing
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("PRAGMA wal_autocheckpoint=0")
    add_memory(holder, "in_wal")
    assert (tmp_path / "ame.db-wal").stat().st_size > 0
    out = backup_database(db, tmp_path / "bk")
    c = sqlite3.connect(str(out))
    assert c.execute("SELECT count(*) FROM memories WHERE memory_id='in_wal'").fetchone()[0] == 1
    c.close()
    holder.close()
    assert inspect(out).integrity == "ok"


def test_e2_restore_roundtrip_saves_current_first(db, tmp_path):
    c = sqlite3.connect(str(db), isolation_level=None)
    add_memory(c, "keep")
    c.close()
    bk = backup_database(db)
    c = sqlite3.connect(str(db), isolation_level=None)
    add_memory(c, "after_backup")
    c.close()
    saved = restore_database(bk, db)
    assert saved is not None and saved.exists() and "pre-restore" in saved.name
    c = sqlite3.connect(str(db))
    ids = {r[0] for r in c.execute("SELECT memory_id FROM memories")}
    c.close()
    assert ids == {"keep"}
    c = sqlite3.connect(str(saved))
    assert {r[0] for r in c.execute("SELECT memory_id FROM memories")} == {"keep", "after_backup"}
    c.close()


def test_e3_backups_are_never_overwritten_or_deleted(db):
    a = backup_database(db)
    b = backup_database(db)
    assert a != b and a.exists() and b.exists()


def test_e4_restore_refuses_corrupt_backup(db, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage" * 1000)
    with pytest.raises(Exception):  # noqa: B017
        restore_database(bad, db)
    assert inspect(db).integrity == "ok"
    with pytest.raises(StorageError):
        restore_database(tmp_path / "missing.db", db)


def test_e5_cli_backup_and_restore(db, tmp_path, capsys):
    assert main(["backup", "--db", str(db), "--dest", str(tmp_path / "out")]) == 0
    (bk,) = (tmp_path / "out").glob("*.db")
    assert main(["restore", str(bk), "--db", str(db)]) == 64  # needs --yes
    assert main(["restore", str(bk), "--db", str(db), "--yes"]) == 0
    assert inspect(db).revision == inspect(bk).revision
    assert main(["backup", "--db", str(tmp_path / "none.db")]) == 3

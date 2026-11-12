"""Step 2: migration 0006 (agents/spaces) - incremental, lossless, guarded (ADR-018)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from ame.application.integrity_service import IntegrityService
from ame.errors import MigrationFailedError
from ame.infrastructure.backup import inspect
from ame.infrastructure.database import MIGRATIONS_DIR
from ame.infrastructure.migration_runner import MigrationRunner
from ame.infrastructure.schema_guards import HEAD, REVISIONS
from tests.phase11.dbutil import MEM_COLS, connect, seeded_0002

HERE = Path(__file__).parent


def test_executed_migrations_0001_0005_byte_identical() -> None:
    for line in (HERE / "executed_migrations.sha256").read_text().splitlines():
        digest, name = line.split()
        data = (MIGRATIONS_DIR / "versions" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name


def test_head_is_0006() -> None:
    assert "0006_agents_spaces" in REVISIONS and HEAD == REVISIONS[-1]


def _seed_05(db: Path) -> dict[str, int]:
    seeded_0002(db)
    MigrationRunner(db).upgrade("0005_conflicts_audit")
    c = connect(db)
    c.execute(
        f"INSERT INTO memories ({MEM_COLS}) VALUES ('p1', 'semantic', 'L2', 'project', "  # noqa: S608
        "'project:capcut', 'u', '2025-12-01T00:00:00Z', '2025-12-01T00:00:00Z', 0.5, 0.5, "
        "'active', 1, '{}')"
    )
    c.execute(
        "INSERT INTO memory_versions (version_id, memory_id, version, content, summary, "
        "content_hash, created_by, created_at, change_reason) VALUES "
        "('ver_p1', 'p1', 1, 'c', NULL, 'h', 'u', 't', NULL)"
    )
    counts = {
        t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("memories", "memory_versions", "memory_edges", "memory_relations",
                  "audit_events", "memory_audit_events", "memory_conflicts")
    }  # fmt: skip
    c.close()
    return counts


def test_upgrade_0005_to_0006_preserves_data_and_backfills(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    before = _seed_05(db)
    rep = MigrationRunner(db).upgrade()
    assert (rep.from_revision, rep.to_revision) == ("0005_conflicts_audit", HEAD)
    assert rep.backup is not None and inspect(rep.backup).revision == "0005_conflicts_audit"
    c = connect(db)
    for t, n in before.items():
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == n, t  # noqa: S608
    spaces = c.execute("SELECT space_id, scope_type, name, owner_agent_id FROM spaces").fetchall()
    assert sorted(spaces) == [
        ("project:capcut", "project", "project:capcut", None),
        ("shared", "shared", "shared", None),
    ]
    created = c.execute("SELECT created_at FROM spaces WHERE space_id='project:capcut'")
    assert created.fetchone()[0] == "2025-12-01T00:00:00Z"
    c.close()
    assert IntegrityService(db).run().exit_code in (0, 1)  # G011 warning possible from seed


def test_scope_type_clash_aborts_and_restores(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _seed_05(db)
    c = connect(db)
    c.execute("UPDATE memories SET scope_type='private' WHERE memory_id='m0'")  # same id, 2 types
    c.close()
    with pytest.raises(MigrationFailedError) as ei:
        MigrationRunner(db).upgrade()
    assert "several scope_types" in ei.value.details["cause"]
    assert inspect(db).revision == "0005_conflicts_audit"
    c = connect(db)
    assert "spaces" not in {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    c.close()


@pytest.fixture()
def head_db(tmp_path: Path) -> Path:
    db = tmp_path / "ame.db"
    MigrationRunner(db).upgrade()
    return db


def test_db_rejects_memory_without_space(head_db: Path) -> None:
    import sqlite3

    c = connect(head_db)
    with pytest.raises(sqlite3.IntegrityError, match="SPACE_NOT_FOUND"):
        c.execute(
            f"INSERT INTO memories ({MEM_COLS}) VALUES ('x', 'semantic', 'L2', 'private', "  # noqa: S608
            "'nowhere', 'u', 't', 't', 0.5, 0.5, 'active', 1, '{}')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="SPACE_NOT_FOUND"):  # type mismatch
        c.execute(
            f"INSERT INTO memories ({MEM_COLS}) VALUES ('x', 'semantic', 'L2', 'private', "  # noqa: S608
            "'shared', 'u', 't', 't', 0.5, 0.5, 'active', 1, '{}')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="delete not allowed"):
        c.execute("DELETE FROM spaces WHERE space_id='shared'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute("UPDATE spaces SET scope_type='private' WHERE space_id='shared'")
    c.execute("UPDATE spaces SET name='Shared' WHERE space_id='shared'")  # name may change
    c.execute(
        "INSERT INTO agents (agent_id, display_name, agent_type, enabled, created_at) "
        "VALUES ('a1', 'A', 'codex', 1, 't')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="delete not allowed"):
        c.execute("DELETE FROM agents")
    c.close()


def test_check_graph_clean_and_detects_dropped_space_trigger(head_db: Path, tmp_path: Path) -> None:
    assert IntegrityService(head_db).run().exit_code == 0
    bad = tmp_path / "bad.db"
    shutil.copy(head_db, bad)
    c = connect(bad)
    c.execute("DROP TRIGGER trg_memories_space_insert")
    c.close()
    rep = IntegrityService(bad).run()
    g7 = [f for f in rep.findings if f.code == "G007"]
    assert g7 and "missing:trg_memories_space_insert" in g7[0].items

"""Phase 3a migrations 0009 (audit rebuild) / 0010 (ACL): integrity on a populated DB."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from ame.application.integrity_service import IntegrityService
from ame.errors import MigrationFailedError
from ame.infrastructure.database import MIGRATIONS_DIR
from ame.infrastructure.migration_runner import MigrationRunner
from ame.infrastructure.schema_guards import HEAD, REVISIONS
from tests.phase11.dbutil import add_memory, connect

ROOT = Path(__file__).resolve().parents[2]
COLS = (
    "event_id, seq, event_type, entity_type, entity_id, memory_id, related_memory_id, "
    "actor_type, actor_id, occurred_at, reason, before_json, after_json, correlation_id"
)


def _cfg(db: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def _audit_fp(db: Path) -> tuple[int, str]:
    c = sqlite3.connect(db)
    h, n = hashlib.sha256(), 0
    for row in c.execute(f"SELECT {COLS} FROM audit_events ORDER BY seq"):  # noqa: S608
        h.update(json.dumps(list(row), ensure_ascii=False).encode() + b"\n")
        n += 1
    c.close()
    return n, h.hexdigest()


def _counts(db: Path) -> dict[str, int]:
    c = sqlite3.connect(db)
    t = ("memories", "memory_versions", "audit_events", "spaces", "agents", "space_members",
         "outbox_events", "memory_fts", "memory_fts_compact")  # fmt: skip
    out = {x: c.execute(f"SELECT count(*) FROM {x}").fetchone()[0] for x in t}  # noqa: S608
    c.close()
    return out


def _populated_0008(db: Path, n: int = 40) -> None:
    MigrationRunner(db).upgrade("0008_cjk_compact_index")
    c = connect(db)
    c.execute(
        "INSERT INTO agents (agent_id, display_name, agent_type, credential_hash, enabled, "
        "created_at) VALUES ('agent:old', 'old', 't', NULL, 1, '2026-01-01T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO spaces (space_id, name, scope_type, owner_agent_id, created_at) "
        "VALUES ('project:old', 'o', 'project', NULL, '2026-01-01T00:00:00Z')"
    )
    c.execute("INSERT INTO space_members (space_id, agent_id, role) "
              "VALUES ('project:old', 'agent:old', 'editor')")  # fmt: skip
    for i in range(n):
        mid = f"m{i:03d}"
        add_memory(c, mid, content=f'第{i}条 部署 流程 "quote" 中文')
        c.execute(
            f"INSERT INTO audit_events ({COLS}) VALUES (?, ?, 'MEMORY_CREATED', 'memory', ?, ?, "  # noqa: S608
            "NULL, 'user', 'user:owner', '2026-01-01T00:00:00Z', ?, NULL, ?, ?)",
            (f"evt_{i:03d}", i + 1, mid, mid, f"创建 {i}", json.dumps({"i": i, "s": "é"}),
             f"cor_{i}"),
        )  # fmt: skip
    c.close()


def test_head_is_0010() -> None:
    # Phase 3b added 0011 on top; 0010 must stay in the chain right before it.
    assert REVISIONS[REVISIONS.index("0010_space_acl") - 1] == "0009_audit_registry"
    assert HEAD == REVISIONS[-1]


def test_audit_rebuild_preserves_every_row(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _populated_0008(db)
    fp0, counts0 = _audit_fp(db), _counts(db)
    rep = MigrationRunner(db).upgrade()
    assert rep.to_revision == HEAD and rep.backup is not None and not rep.restored
    assert _audit_fp(db) == fp0 and fp0[0] == 40
    assert _counts(db) == counts0
    c = connect(db)
    # append-only triggers survived the rebuild
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE audit_events SET reason = 'x' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM audit_events WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM space_members")
    # widened CHECK accepts the new types; backfilled grant metadata
    c.execute(
        f"INSERT INTO audit_events ({COLS}) VALUES ('evt_n', 999, 'SPACE_CREATED', "  # noqa: S608
        "'space', 's', NULL, NULL, 'user', 'u', 't', 'r', NULL, NULL, 'c')"
    )
    row = c.execute("SELECT granted_at, granted_by FROM space_members").fetchone()
    assert row[0] and row[1] == "migration"
    idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                   "AND tbl_name='audit_events' AND name LIKE 'ix_%'")}  # fmt: skip
    c.close()
    assert idx == {"ix_audit_events_memory", "ix_audit_events_related",
                   "ix_audit_events_entity", "ix_audit_events_correlation"}  # fmt: skip
    r = IntegrityService(db).run()
    assert r.exit_code == 0 and "G014" in r.checked


def test_upgrade_downgrade_upgrade_populated(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _populated_0008(db)
    fp0, counts0 = _audit_fp(db), _counts(db)
    MigrationRunner(db).upgrade()
    command.downgrade(_cfg(db), "0008_cjk_compact_index")
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    c = connect(db)
    cols = {r[1] for r in c.execute("PRAGMA table_info(space_members)")}
    assert "granted_at" not in cols
    with pytest.raises(sqlite3.IntegrityError):  # old CHECK is back
        c.execute(
            f"INSERT INTO audit_events ({COLS}) VALUES ('e', 999, 'SPACE_CREATED', "  # noqa: S608
            "'space', 's', NULL, NULL, 'user', 'u', 't', 'r', NULL, NULL, 'c')"
        )
    c.close()
    assert IntegrityService(db).run().exit_code == 0
    MigrationRunner(db).upgrade()
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert IntegrityService(db).run().exit_code == 0


def test_downgrade_refused_once_new_audit_types_exist(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _populated_0008(db)
    MigrationRunner(db).upgrade()
    c = connect(db)
    c.execute(
        f"INSERT INTO audit_events ({COLS}) VALUES ('evt_n', 999, 'AGENT_REGISTERED', "  # noqa: S608
        "'agent', 'a', NULL, NULL, 'user', 'u', 't', 'r', NULL, NULL, 'c')"
    )
    c.close()
    fp = _audit_fp(db)
    with pytest.raises(Exception, match="downgrade refused"):
        command.downgrade(_cfg(db), "0008_cjk_compact_index")
    assert _audit_fp(db) == fp  # nothing dropped


def test_injected_failure_restores_backup(tmp_path: Path) -> None:
    """A view named audit_events_new makes 0009's DROP TABLE IF EXISTS fail mid-migration;
    the runner must restore the automatic backup with every audit row intact."""
    db = tmp_path / "ame.db"
    _populated_0008(db)
    c = connect(db)
    c.execute("CREATE VIEW audit_events_new AS SELECT 1 AS x")
    c.close()
    fp0, counts0 = _audit_fp(db), _counts(db)
    with pytest.raises(MigrationFailedError) as ei:
        MigrationRunner(db).upgrade()
    assert "restored" in ei.value.message
    c = connect(db)
    rev = c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    c.close()
    assert rev == "0008_cjk_compact_index"
    assert _audit_fp(db) == fp0 and _counts(db) == counts0


def test_fingerprint_mismatch_aborts_copy(tmp_path: Path) -> None:
    """Unit-level: if the copied table's fingerprint differs, rebuild raises before the old
    table is dropped (transaction rolled back; old rows untouched)."""
    db = tmp_path / "ame.db"
    _populated_0008(db, n=5)
    fp0 = _audit_fp(db)
    spec = importlib.util.spec_from_file_location(
        "m0009", MIGRATIONS_DIR / "versions" / "0009_audit_registry.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real = mod.fingerprint
    calls = {"n": 0}

    def fake(bind, table):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        n, h = real(bind, table)
        return (n, h if table == "audit_events" else "tampered")

    mod.fingerprint = fake
    eng = sa.create_engine(f"sqlite:///{db}")
    with pytest.raises(RuntimeError, match="verification failed"):
        with eng.begin() as conn:
            mod.rebuild(conn, mod.OLD_EVENTS + mod.NEW_EVENTS, mod.OLD_ENTITIES + mod.NEW_ENTITIES)
    eng.dispose()
    assert calls["n"] == 2 and _audit_fp(db) == fp0


def test_executed_migrations_0001_0010_hashes() -> None:
    lines = (ROOT / "tests/phase12/executed_migrations.sha256").read_text().splitlines()
    names = []
    for line in lines:
        digest, name = line.split()
        p = ROOT / "migrations/versions" / name
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest, name
        names.append(name)
    assert any(n.startswith("0009_") for n in names) and any(n.startswith("0010_") for n in names)

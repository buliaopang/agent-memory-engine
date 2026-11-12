"""E: populated 0002 database -> 0003 -> 0004 -> 0005 keeps every row; backfills work;
0004 aborts on pre-existing cycles; automatic backup + auto-restore on failure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ame.errors import MigrationFailedError
from ame.infrastructure.backup import inspect
from ame.infrastructure.migration_runner import MigrationRunner
from ame.infrastructure.schema_guards import HEAD, required_triggers
from tests.phase11.dbutil import add_edge_0002, connect, seeded_0002


def test_e1_populated_0002_upgrades_to_head_without_loss(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    before = seeded_0002(db)
    rep = MigrationRunner(db).upgrade()
    assert (rep.from_revision, rep.to_revision) == ("0002_core_memory_tables", HEAD)
    assert rep.backup is not None and rep.backup.exists()
    assert inspect(rep.backup).revision == "0002_core_memory_tables"
    after = inspect(db)
    assert after.integrity == "ok" and after.revision == HEAD
    assert after.counts["memories"] == before["memories"]
    assert after.counts["memory_versions"] == before["memory_versions"]
    # SUPPORTS edge moved to relations by 0003: link total preserved
    assert (
        after.counts["memory_edges"] + after.counts["memory_relations"]
        == before["memory_edges"] + before["memory_relations"]
    )
    assert after.counts["memory_audit_events"] == before["memory_audit_events"]  # frozen, kept
    c = connect(db)
    trig = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert set(required_triggers(HEAD)) <= trig
    # legacy audit back-filled into audit_events in original order
    rows = c.execute(
        "SELECT seq, event_type, memory_id, before_json, after_json FROM audit_events "
        "WHERE json_extract(after_json, '$.legacy') = 1 ORDER BY seq"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]
    assert [r[1] for r in rows] == [
        "MEMORY_CREATED", "MEMORY_CREATED", "MEMORY_STATUS_CHANGED", "MEMORY_CREATED",
        "MEMORY_STATUS_CHANGED",
    ]  # fmt: skip
    assert json.loads(rows[2][3]) == {"status": "active"}
    # conflicted pair with an active CONTRADICTS gets an open conflict back-filled
    cf = c.execute("SELECT relation_id, memory_a_id, memory_b_id, status FROM memory_conflicts")
    assert cf.fetchall() == [("r1", "cA", "cB", "open")]
    c.close()


def test_e1b_upgraded_db_is_usable_by_services(tmp_path: Path) -> None:
    from ame.application.memory_service import MemoryService
    from ame.application.schemas import ConflictResolve, StatusChange
    from ame.config import Settings
    from ame.infrastructure.database import create_sqlite_engine
    from ame.infrastructure.unit_of_work import make_session_factory
    from tests.conftest import USER

    s = Settings(data_dir=tmp_path)
    seeded_0002(s.db_path)
    MigrationRunner(s.db_path).upgrade()
    eng = create_sqlite_engine(s)
    svc = MemoryService(make_session_factory(eng))
    # T10 restore uses the back-filled legacy audit to find the pre-archive status
    assert (
        svc.change_status("arch", StatusChange(to_status="active", reason="r"), USER).status
        == "active"
    )
    (cf,) = svc.list_conflicts("cA")
    out = svc.resolve_conflict(
        cf.conflict_id,
        ConflictResolve(resolution="kept_both", reason="both true", evidence={"note": "checked"}),
        USER,
    )
    assert out.status == "resolved"
    assert svc.get_memory("cA").status == "active" and svc.get_memory("cB").status == "active"
    eng.dispose()


def test_a1_0004_aborts_on_existing_cycle_and_db_is_restored(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    seeded_0002(db)
    c = connect(db)
    add_edge_0002(c, "cyc", "m2", "m0")  # m0->m1->m2->m0
    c.close()
    with pytest.raises(MigrationFailedError) as ei:
        MigrationRunner(db).upgrade()
    assert "cycle" in ei.value.details["cause"].lower()
    info = inspect(db)
    assert info.revision == "0002_core_memory_tables" and info.integrity == "ok"
    backups = sorted((tmp_path / "backups").glob("*.db"))
    assert any("pre-migrate" in p.name for p in backups)
    assert any("pre-restore" in p.name for p in backups)  # failed state kept for analysis


def test_e2_failure_in_post_check_triggers_auto_restore(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ame.db"
    seeded_0002(db)
    real = MigrationRunner._alembic_upgrade

    def sabotage(self: MigrationRunner, target: str) -> None:
        real(self, target)
        c = connect(db)
        c.execute("DROP TRIGGER trg_audit_events_no_update")
        c.close()

    monkeypatch.setattr(MigrationRunner, "_alembic_upgrade", sabotage)
    with pytest.raises(MigrationFailedError) as ei:
        MigrationRunner(db).upgrade()
    assert "missing triggers" in json.dumps(ei.value.details)
    assert inspect(db).revision == "0002_core_memory_tables"


def test_e3_already_at_head_is_noop_without_backup(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    MigrationRunner(db).upgrade()
    rep = MigrationRunner(db).upgrade()
    assert rep.backup is None and rep.notes == ["already at target"]


def test_e4_corrupt_db_refused_before_any_change(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    db.write_bytes(b"not a database" * 100)
    with pytest.raises(Exception):  # noqa: B017 - sqlite raises DatabaseError on open
        MigrationRunner(db).upgrade()
    assert db.read_bytes().startswith(b"not a database")

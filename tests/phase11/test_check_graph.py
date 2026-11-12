"""ame check-graph (ADR-011): G001-G011, exit codes, read-only guarantee."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ame.cli import main
from ame.infrastructure.migration_runner import MigrationRunner
from tests.phase11.dbutil import add_memory, connect

EDGE = (
    "INSERT INTO memory_edges (edge_id, from_memory_id, to_memory_id, edge_type, status, "
    "created_by, created_at, metadata_json) VALUES (?, ?, ?, ?, 'active', 'u', 't', '{}')"
)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "ame.db"
    MigrationRunner(p).upgrade()
    c = connect(p)
    for m in ("a", "b", "c"):
        add_memory(c, m)
    c.execute(EDGE, ("e1", "a", "b", "PARENT"))
    c.close()
    return p


def run(capsys, *args: str) -> tuple[int, str]:
    rc = main(list(args))
    return rc, capsys.readouterr().out


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_clean_db_exit_0(db, capsys):
    rc, out = run(capsys, "check-graph", "--db", str(db))
    assert rc == 0 and "OK:" in out


def _break_everything(db: Path) -> None:
    """Simulate a tampered DB: drop guards, then insert what they would have stopped."""
    c = connect(db)
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("DROP TRIGGER trg_memory_edges_no_cycle_insert")
    c.execute("DROP TRIGGER trg_memory_edges_endpoints_insert")
    c.execute(EDGE, ("e2", "b", "a", "REFERENCE"))  # G001 cycle
    c.execute(EDGE, ("e3", "c", "ghost", "PARENT"))  # G003 dangling
    c.execute("UPDATE memories SET status='superseded' WHERE memory_id='c'")  # G006
    c.close()


def test_broken_db_reports_errors_exit_2_and_is_not_modified(db, capsys):
    _break_everything(db)
    before = _sha(db)
    rc, out = run(capsys, "check-graph", "--db", str(db), "--verbose", "--repair-dry-run")
    assert rc == 2
    for code in ("G001", "G003", "G006", "G007", "G008", "G009"):
        assert code in out
    assert "missing:trg_memory_edges_no_cycle_insert" in out
    assert "repair (dry-run, nothing changed)" in out
    assert _sha(db) == before  # read-only by default


def test_json_output(db, capsys):
    _break_everything(db)
    rc, out = run(capsys, "check-graph", "--db", str(db), "--json")
    data = json.loads(out)
    assert rc == data["exit_code"] == 2
    assert {f["code"] for f in data["findings"]} >= {"G001", "G003", "G007"}


def test_warning_only_exit_1(db, capsys):
    c = connect(db)
    c.execute("UPDATE memories SET status='superseded' WHERE memory_id='c'")
    c.close()
    rc, out = run(capsys, "check-graph", "--db", str(db))
    assert rc == 1 and "G006" in out


def test_g007_modified_trigger_detected(db, capsys):
    c = connect(db)
    c.execute("DROP TRIGGER trg_audit_events_no_update")
    c.execute("CREATE TRIGGER trg_audit_events_no_update BEFORE UPDATE ON audit_events "
              "BEGIN SELECT 1; END")  # fmt: skip
    c.close()
    rc, out = run(capsys, "check-graph", "--db", str(db), "-v")
    assert rc == 2 and "changed:trg_audit_events_no_update" in out


def test_g011_conflict_bookkeeping(db, capsys):
    c = connect(db)
    c.execute("DROP TRIGGER trg_memories_conflicted_requires_evidence")
    c.execute("UPDATE memories SET status='conflicted' WHERE memory_id='a'")
    c.close()
    rc, out = run(capsys, "check-graph", "--db", str(db), "-v")
    assert "G011" in out and "conflicted memory has no open conflict" in out


def test_old_revision_exit_3(tmp_path, capsys):
    p = tmp_path / "old.db"
    MigrationRunner(p).upgrade("0003_link_revocation")
    rc, out = run(capsys, "check-graph", "--db", str(p))
    assert rc == 3 and "run migrations first" in out


def test_missing_db_exit_3(tmp_path, capsys):
    rc, _ = run(capsys, "check-graph", "--db", str(tmp_path / "nope.db"))
    assert rc == 3
    assert not (tmp_path / "nope.db").exists()


def test_bad_arguments_exit_64(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["check-graph", "--no-such-flag"])
    assert ei.value.code == 64
    with pytest.raises(SystemExit) as ei:
        main([])
    assert ei.value.code == 64

"""Step 1: migration 0007 (FTS + outbox + vector state) - incremental, lossless, guarded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ame.application.integrity_service import IntegrityService
from ame.application.memory_service import MemoryService
from ame.application.schemas import MemoryPatch, StatusChange, VersionCreate
from ame.domain.enums import MemoryStatus
from ame.errors import MigrationFailedError
from ame.infrastructure.backup import inspect
from ame.infrastructure.migration_runner import MigrationRunner
from ame.infrastructure.schema_guards import HEAD
from tests.conftest import USER, make
from tests.phase11.dbutil import add_memory, connect

TABLES = ("memories", "memory_versions", "memory_edges", "memory_relations", "audit_events",
          "memory_audit_events", "memory_conflicts", "spaces")  # fmt: skip


def _seed_06(db: Path) -> dict[str, int]:
    MigrationRunner(db).upgrade("0006_agents_spaces")
    c = connect(db)
    add_memory(c, "m1", content="桌面自动化流程")
    add_memory(c, "m2", content="other text")
    c.execute("INSERT INTO memory_tags (memory_id, tag) VALUES ('m1', 'zeta'), ('m1', 'alpha')")
    counts = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}  # noqa: S608
    c.close()
    return counts


def test_head_is_0007() -> None:
    # Phase 2.1 (ADR-023): head moved to 0008; 0007 must still be in the chain.
    from ame.infrastructure.schema_guards import REVISIONS

    assert "0007_search_outbox" in REVISIONS and HEAD == REVISIONS[-1]


def test_upgrade_0006_to_0007_backfills_without_loss(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    before = _seed_06(db)
    rep = MigrationRunner(db).upgrade()
    assert (rep.from_revision, rep.to_revision) == ("0006_agents_spaces", HEAD)
    assert rep.backup is not None and inspect(rep.backup).revision == "0006_agents_spaces"
    c = connect(db)
    for t, n in before.items():
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == n, t  # noqa: S608
    fts = c.execute("SELECT memory_id, version, tags FROM memory_fts ORDER BY memory_id").fetchall()
    assert fts == [("m1", 1, "alpha zeta"), ("m2", 1, "")]
    hit = c.execute("SELECT memory_id FROM memory_fts WHERE memory_fts MATCH '\"自动化\"'")
    assert hit.fetchall() == [("m1",)]
    ev = c.execute("SELECT event_type, aggregate_id, status, payload_json FROM outbox_events")
    rows = sorted(ev.fetchall())
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("memory.index", "m1", "pending"), ("memory.index", "m2", "pending")
    ]  # fmt: skip
    assert json.loads(rows[0][3]) == {"memory_id": "m1", "version": 1}
    c.close()
    assert IntegrityService(db).run().exit_code == 0


def test_failed_0007_restores_backup(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    before = _seed_06(db)
    c = connect(db)
    c.execute("CREATE TABLE memory_fts (x TEXT)")  # makes CREATE VIRTUAL TABLE fail
    c.close()
    with pytest.raises(MigrationFailedError):
        MigrationRunner(db).upgrade()
    after = inspect(db)
    assert after.revision == "0006_agents_spaces"
    c = connect(db)
    for t, n in before.items():
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == n, t  # noqa: S608
    c.close()


def _fts(db: Path, mid: str) -> tuple[object, ...]:
    c = connect(db)
    row = c.execute(
        "SELECT version, content, summary, tags FROM memory_fts WHERE memory_id=?", (mid,)
    ).fetchall()
    c.close()
    assert len(row) == 1
    return tuple(row[0])


def test_fts_follows_versions_and_tags(svc: MemoryService, settings) -> None:  # type: ignore[no-untyped-def]
    db = settings.db_path
    m = svc.create_memory(make(content="第一版正文", summary="摘要", tags=["b", "a"]), USER)
    assert _fts(db, m.memory_id) == (1, "第一版正文", "摘要", "a b")
    svc.create_version(
        m.memory_id, VersionCreate(content="第二版正文", change_reason="r", expected_version=1),
        USER,
    )  # fmt: skip
    assert _fts(db, m.memory_id)[:2] == (2, "第二版正文")
    svc.patch_memory(m.memory_id, MemoryPatch(tags=["c"], reason="r"), USER)
    assert _fts(db, m.memory_id)[3] == "c"
    assert IntegrityService(db).run().exit_code == 0


def test_outbox_events_same_transaction_ids_only(svc: MemoryService, settings) -> None:  # type: ignore[no-untyped-def]
    m = svc.create_memory(make(content="秘密正文 secret"), USER)
    svc.change_status(m.memory_id, StatusChange(to_status=MemoryStatus.ARCHIVED, reason="r"), USER)
    c = connect(settings.db_path)
    rows = c.execute(
        "SELECT event_type, payload_json FROM outbox_events WHERE aggregate_id=? ORDER BY rowid",
        (m.memory_id,),
    ).fetchall()
    assert [r[0] for r in rows] == ["memory.index", "memory.status"]
    assert all("secret" not in r[1] and "秘密" not in r[1] for r in rows)
    with pytest.raises(Exception, match="delete not allowed"):
        c.execute("DELETE FROM outbox_events")
    c.close()


def test_check_graph_g012_and_g008_detect_tampering(svc: MemoryService, settings) -> None:  # type: ignore[no-untyped-def]
    db = settings.db_path
    m = svc.create_memory(make(content="abc def"), USER)
    rep = IntegrityService(db).run()
    assert "G012" in rep.checked and rep.exit_code == 0
    c = connect(db)
    c.execute("DELETE FROM memory_fts WHERE memory_id=?", (m.memory_id,))
    c.close()
    rep = IntegrityService(db).run()
    assert any(f.code == "G012" for f in rep.findings) and rep.exit_code == 2
    c = connect(db)
    c.execute("DROP TRIGGER trg_memory_versions_fts_sync")
    c.close()
    codes = {f.code for f in IntegrityService(db).run().findings}
    assert {"G007", "G008"} <= codes

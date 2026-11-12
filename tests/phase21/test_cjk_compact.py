"""Phase 2.1 (ADR-023): whitespace-insensitive CJK matching via memory_fts_compact."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from ame.application.index_worker import IndexWorker
from ame.application.integrity_service import IntegrityService
from ame.application.memory_service import MemoryService
from ame.application.schemas import SearchRequest, VersionCreate
from ame.application.search_service import SearchService
from ame.domain.text_normalize import STRIP_CHARS, compact, compact_sql
from ame.infrastructure.database import MIGRATIONS_DIR
from ame.infrastructure.migration_runner import MigrationRunner
from ame.infrastructure.schema_guards import HEAD
from ame.infrastructure.unit_of_work import make_session_factory
from tests.conftest import USER, make
from tests.phase2.conftest import NOW
from tests.phase11.dbutil import add_memory, connect

ROOT = Path(__file__).resolve().parents[2]


def q(text: str) -> SearchRequest:
    return SearchRequest.model_validate({"query": text})


def ids(resp) -> set[str]:  # type: ignore[no-untyped-def]
    return {r.memory.memory_id for r in resp.results}


def test_head_is_0008() -> None:
    # Phase 3a: head moved past 0008; 0008 must still be in the chain.
    from ame.infrastructure.schema_guards import REVISIONS

    assert "0008_cjk_compact_index" in REVISIONS and HEAD == REVISIONS[-1]


@pytest.mark.parametrize(
    "raw", ["部署 流程", "部署\n流程", "部署\u3000流程", "部署\t流程", "部署\r\n流程"]
)
def test_compact_matches_sql(raw: str) -> None:
    c = sqlite3.connect(":memory:")
    (got,) = c.execute(f"SELECT {compact_sql('?')}", (raw,)).fetchone()
    assert got == compact(raw) == "部署流程"


def test_compact_keeps_punctuation_and_other_text() -> None:
    assert compact("部署，流程") == "部署，流程"
    assert compact("deploy flow") == "deployflow"
    assert set(STRIP_CHARS) == {" ", "\u3000", "\t", "\n", "\r"}


@pytest.mark.parametrize("sep", [" ", "\n", "\u3000", "\t"])
def test_query_without_space_finds_spaced_text(
    svc: MemoryService, search: SearchService, sep: str
) -> None:
    m = svc.create_memory(make(content=f"上线前的部署{sep}流程说明"), USER)
    svc.create_memory(make(content="完全无关"), USER)
    assert ids(search.search(q("部署流程"), NOW)) == {m.memory_id}
    assert ids(search.search(q("署流"), NOW)) == {m.memory_id}  # 2 chars -> LIKE on compact


def test_query_with_space_finds_unspaced_text(svc: MemoryService, search: SearchService) -> None:
    m = svc.create_memory(make(content="上线前的部署流程说明"), USER)
    assert ids(search.search(q("部署 流程"), NOW)) == {m.memory_id}
    assert ids(search.search(q("部署　流程"), NOW)) == {m.memory_id}


def test_summary_is_compacted(svc: MemoryService, search: SearchService) -> None:
    m = svc.create_memory(make(content="x", summary="回滚 方案"), USER)
    assert ids(search.search(q("回滚方案"), NOW)) == {m.memory_id}


def test_punctuation_still_separates(svc: MemoryService, search: SearchService) -> None:
    svc.create_memory(make(content="部署，流程"), USER)
    assert ids(search.search(q("部署流程"), NOW)) == set()


def test_english_unchanged(svc: MemoryService, search: SearchService) -> None:
    a = svc.create_memory(make(content="Desktop Automation checklist"), USER)
    b = svc.create_memory(make(content="desktopautomation tool"), USER)
    assert ids(search.search(q("desktop"), NOW)) == {a.memory_id, b.memory_id}
    assert a.memory_id in ids(search.search(q("automation"), NOW))
    # documented side effect (C24): compact form also matches across the space
    assert ids(search.search(q("desktopautomation"), NOW)) == {a.memory_id, b.memory_id}


def test_new_version_resyncs(svc: MemoryService, search: SearchService) -> None:
    m = svc.create_memory(make(content="旧的 内容"), USER)
    svc.create_version(
        m.memory_id,
        VersionCreate(content="新的 部署 流程", change_reason="r", expected_version=1),
        USER,
    )
    assert ids(search.search(q("部署流程"), NOW)) == {m.memory_id}
    assert ids(search.search(q("旧的内容"), NOW)) == set()


def test_g013_detects_and_rebuild_repairs(svc: MemoryService, settings, engine) -> None:  # type: ignore[no-untyped-def]
    db = settings.db_path
    m = svc.create_memory(make(content="部署 流程"), USER)
    rep = IntegrityService(db).run()
    assert "G013" in rep.checked and rep.exit_code == 0
    c = connect(db)
    c.execute("DELETE FROM memory_fts_compact WHERE memory_id=?", (m.memory_id,))
    c.close()
    rep = IntegrityService(db).run()
    assert any(f.code == "G013" for f in rep.findings) and rep.exit_code == 2
    IndexWorker(make_session_factory(engine)).rebuild_lexical()
    assert IntegrityService(db).run().exit_code == 0
    c = connect(db)
    c.execute("DROP TRIGGER trg_memory_versions_fts_compact_sync")
    c.close()
    rep = IntegrityService(db).run()
    assert any(f.code == "G007" for f in rep.findings)


def _cfg(db: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def _snapshot(db: Path) -> dict[str, int]:
    c = connect(db)
    t = ("memories", "memory_versions", "memory_tags", "audit_events", "memory_audit_events",
         "outbox_events", "memory_fts")  # fmt: skip
    out = {x: c.execute(f"SELECT count(*) FROM {x}").fetchone()[0] for x in t}  # noqa: S608
    c.close()
    return out


def test_upgrade_downgrade_upgrade_populated(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    MigrationRunner(db).upgrade("0007_search_outbox")
    c = connect(db)
    for i in range(50):
        add_memory(c, f"m{i:02d}", content=f"第{i}条 部署 流程")
    c.close()
    before = _snapshot(db)
    rep = MigrationRunner(db).upgrade()
    assert rep.to_revision == HEAD and rep.backup is not None
    c = connect(db)
    assert c.execute("SELECT count(*) FROM memory_fts_compact").fetchone()[0] == 50
    hit = c.execute("SELECT count(*) FROM memory_fts_compact WHERE memory_fts_compact MATCH ?",
                    ('"部署流程"',)).fetchone()[0]  # fmt: skip
    assert hit == 50
    c.close()
    assert _snapshot(db) == before
    assert IntegrityService(db).run().exit_code == 0
    command.downgrade(_cfg(db), "0007_search_outbox")
    c = connect(db)
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'memory_fts_compact%'"
                     ).fetchone()[0] == 0  # fmt: skip
    c.close()
    assert _snapshot(db) == before
    assert IntegrityService(db).run().exit_code == 0
    MigrationRunner(db).upgrade()
    assert _snapshot(db) == before
    assert IntegrityService(db).run().exit_code == 0


def test_executed_migrations_unchanged() -> None:
    lines = (ROOT / "tests/phase12/executed_migrations.sha256").read_text().splitlines()
    names = []
    for line in lines:
        digest, name = line.split()
        names.append(name)
        data = (ROOT / "migrations/versions" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name
    assert "0007_search_outbox.py" in names

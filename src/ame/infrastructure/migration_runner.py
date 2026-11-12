"""Safe schema upgrades (ADR-014).

1. pre-check (integrity_check, foreign_key_check) on an existing database
2. automatic backup with the SQLite backup API (includes WAL content), verified
3. alembic upgrade
4. post-check: integrity_check, revision, required guard triggers present, no new FK
   violations, row counts of pre-existing tables did not shrink
5. any failure -> the pre-migration backup is restored automatically (the failed state is
   kept as pre-restore-*.db) and MigrationFailedError is raised.

Structural downgrade is NOT offered for 0003/0005; data recovery = restore a backup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from alembic.config import Config

from ame.errors import MigrationFailedError
from ame.infrastructure.backup import backup_database, inspect, restore_database
from ame.infrastructure.database import MIGRATIONS_DIR, check_sqlite_version
from ame.infrastructure.schema_guards import HEAD, required_triggers


@dataclass
class MigrationReport:
    from_revision: str | None
    to_revision: str | None
    backup: Path | None = None
    restored: bool = False
    notes: list[str] = field(default_factory=list)


def _fk_violations(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return len(con.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        con.close()


def _triggers(db: Path) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        con.close()


class MigrationRunner:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _alembic_upgrade(self, target: str) -> None:
        cfg = Config()
        cfg.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path.resolve().as_posix()}")
        command.upgrade(cfg, target)

    def upgrade(self, target: str = "head") -> MigrationReport:
        check_sqlite_version()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        want = HEAD if target == "head" else target
        fresh = not self.db_path.exists() or self.db_path.stat().st_size == 0
        before = None if fresh else inspect(self.db_path)
        report = MigrationReport(before.revision if before else None, want)
        if before is not None and before.revision == want:
            report.notes.append("already at target")
            return report
        fk_before = 0
        if before is not None:
            if before.integrity != "ok":
                raise MigrationFailedError(
                    "Pre-migration integrity_check failed; nothing changed",
                    {"integrity": before.integrity[:200]},
                )
            fk_before = _fk_violations(self.db_path)
            report.backup = backup_database(self.db_path, label="pre-migrate")
        try:
            self._alembic_upgrade(target)
            self._post_check(want, before.counts if before else {}, fk_before)
        except Exception as exc:
            if report.backup is None:
                raise MigrationFailedError(
                    "Migration of a new database failed", {"cause": type(exc).__name__}
                ) from exc
            restore_database(report.backup, self.db_path)
            report.restored = True
            raise MigrationFailedError(
                "Migration failed; the pre-migration backup was restored",
                {
                    "cause": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "backup": str(report.backup),
                    "restored_revision": before.revision if before else None,
                    "problems": exc.details.get("problems", [])
                    if isinstance(exc, MigrationFailedError)
                    else [],
                },
            ) from exc
        return report

    def _post_check(self, want: str, counts_before: dict[str, int], fk_before: int) -> None:
        after = inspect(self.db_path)
        problems: list[str] = []
        if after.integrity != "ok":
            problems.append("integrity_check failed")
        if after.revision != want:
            problems.append(f"revision is {after.revision}, expected {want}")
        missing = sorted(set(required_triggers(want)) - _triggers(self.db_path))
        if missing:
            problems.append(f"missing triggers {missing}")
        if _fk_violations(self.db_path) > fk_before:
            problems.append("new foreign key violations")
        # 0003 moves SUPPORTS edges into relations: compare the combined link count.
        links_b = counts_before.get("memory_edges", 0) + counts_before.get("memory_relations", 0)
        links_a = after.counts.get("memory_edges", 0) + after.counts.get("memory_relations", 0)
        if links_a < links_b:
            problems.append("edges+relations row count decreased")
        for t, n in counts_before.items():
            if t in ("memory_edges", "memory_relations"):
                continue
            if after.counts.get(t, 0) < n:
                problems.append(f"row count of {t} decreased")
        if problems:
            raise MigrationFailedError("Post-migration check failed", {"problems": problems})

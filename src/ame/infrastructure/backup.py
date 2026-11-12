"""Online backup / restore with the SQLite backup API (ADR-014).

The backup API copies committed pages *including those still in the -wal file*; copying
only the .db file of a WAL database can silently lose data (review finding N10).
Backups are never overwritten or deleted (Q-E1).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ame.errors import StorageError

BACKUP_DIRNAME = "backups"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    revision: str | None
    integrity: str
    counts: dict[str, int]


COUNT_TABLES = (
    "memories", "memory_versions", "memory_edges", "memory_relations", "memory_tags",
    "memory_sources", "memory_audit_events", "memory_conflicts", "audit_events",
)  # fmt: skip


def inspect(path: Path) -> BackupInfo:
    con = _connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        rev = None
        if "alembic_version" in tables:
            row = con.execute("SELECT version_num FROM alembic_version").fetchone()
            rev = row[0] if row else None
        counts = {
            t: int(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # noqa: S608
            for t in COUNT_TABLES
            if t in tables
        }
        return BackupInfo(path, rev, integrity, counts)
    finally:
        con.close()


def _copy(src: Path, dst: Path) -> None:
    s = _connect(src)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def backup_database(db_path: Path, dest_dir: Path | None = None, label: str = "manual") -> Path:
    if not db_path.exists():
        raise StorageError("Database file does not exist", {"db": db_path.name})
    out_dir = dest_dir or db_path.parent / BACKUP_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    rev = inspect(db_path).revision or "none"
    target = out_dir / f"ame-{label}-{rev}-{_stamp()}.db"
    if target.exists():  # never overwrite a backup
        raise StorageError("Backup target already exists", {"backup": target.name})
    _copy(db_path, target)
    info = inspect(target)
    if info.integrity != "ok":
        raise StorageError("Backup failed integrity_check", {"backup": target.name})
    return target


def restore_database(backup: Path, db_path: Path) -> Path | None:
    """Restore `backup` into `db_path`. The current DB is first saved as pre-restore-*.db.

    Stop the AME service before restoring. Returns the path of the pre-restore copy.
    """
    if not backup.exists():
        raise StorageError("Backup file does not exist", {"backup": backup.name})
    if inspect(backup).integrity != "ok":
        raise StorageError("Backup failed integrity_check; refusing to restore")
    saved: Path | None = None
    if db_path.exists():
        saved = backup_database(db_path, label="pre-restore")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _copy(backup, db_path)
    if inspect(db_path).integrity != "ok":
        raise StorageError("Restored database failed integrity_check")
    return saved

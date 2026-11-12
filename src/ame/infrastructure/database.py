"""SQLite engine, PRAGMAs (WAL + foreign keys), transaction mode, migrations and health probe."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text

from ame.config import Settings
from ame.errors import StorageError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REPO_MIGRATIONS = PROJECT_ROOT / "migrations"
_PKG_MIGRATIONS = Path(__file__).resolve().parents[1] / "_migrations"
MIGRATIONS_DIR = _REPO_MIGRATIONS if _REPO_MIGRATIONS.exists() else _PKG_MIGRATIONS
MIN_SQLITE = (3, 35, 0)  # Q-F1: partial indexes, RETURNING, window functions, recursive CTE

# ADR-010 / Q-A2: write use cases open their transaction with BEGIN IMMEDIATE so the
# write lock is taken *before* any read (check-then-write is atomic and writers queue on
# busy_timeout instead of failing with SQLITE_BUSY_SNAPSHOT). Reads stay DEFERRED.
_BEGIN_MODE: ContextVar[str] = ContextVar("ame_begin_mode", default="DEFERRED")


def check_sqlite_version(version: tuple[int, int, int] | None = None) -> None:
    v = version or sqlite3.sqlite_version_info
    if tuple(v) < MIN_SQLITE:
        raise StorageError(
            f"SQLite {'.'.join(map(str, v))} is too old; AME needs >= 3.35.0",
            {"sqlite_version": ".".join(map(str, v))},
        )


@contextmanager
def begin_mode(mode: str) -> Iterator[None]:
    token = _BEGIN_MODE.set(mode)
    try:
        yield
    finally:
        _BEGIN_MODE.reset(token)


def create_sqlite_engine(settings: Settings) -> Engine:
    check_sqlite_version()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
        dbapi_conn.isolation_level = None  # let SQLAlchemy's "begin" event emit BEGIN
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE" if _BEGIN_MODE.get() == "IMMEDIATE" else "BEGIN")

    return engine


def run_migrations(settings: Settings) -> None:
    """Upgrade to head through the safe runner (backup, pre/post checks, auto-restore)."""
    from ame.infrastructure.migration_runner import MigrationRunner

    MigrationRunner(settings.db_path).upgrade()


class DatabaseHealthProbe:
    """Infrastructure-level probe; keeps SQL out of routes."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def check(self) -> dict[str, Any]:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                mode = conn.execute(text("PRAGMA journal_mode")).scalar()
                fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
                rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            return {
                "status": "ok",
                "journal_mode": str(mode),
                "foreign_keys": bool(fk),
                "schema_revision": rev,
            }
        except Exception as exc:  # report, never leak paths/secrets
            return {"status": "error", "error": type(exc).__name__}

from sqlalchemy import text

from ame.infrastructure.database import DatabaseHealthProbe, create_sqlite_engine, run_migrations


def test_init_creates_db_with_wal_and_fk(settings):
    run_migrations(settings)
    assert settings.db_path.exists()
    engine = create_sqlite_engine(settings)
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "0016_single_parent"
        )
    engine.dispose()


def test_migrations_idempotent(settings):
    run_migrations(settings)
    run_migrations(settings)


def test_probe_reports_error_when_unmigrated(settings):
    engine = create_sqlite_engine(settings)
    assert DatabaseHealthProbe(engine).check()["status"] == "error"
    engine.dispose()

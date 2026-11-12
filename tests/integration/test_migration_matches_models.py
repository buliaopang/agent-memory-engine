from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from ame.infrastructure.models import Base


def _not_fts(name, type_, parent_names):
    # Phase 2 (ADR-020): memory_fts is an FTS5 virtual table (and its shadow tables) created
    # by raw SQL in 0007; it has no ORM model. Everything else must still match exactly.
    return not (type_ == "table" and name is not None and name.startswith("memory_fts"))


def test_migration_schema_matches_orm_models(engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_name": _not_fts})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_head_revision(engine):
    from sqlalchemy import text

    with engine.connect() as c:
        rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert rev == "0016_single_parent"

"""Phase 0 baseline: schema metadata only. Business tables arrive in Phase 1.

Revision ID: 0001_baseline
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ame_meta",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),  # UTC ISO-8601
    )
    op.execute(
        "INSERT INTO ame_meta(key, value, updated_at) VALUES "
        "('schema_owner', 'ame', strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
    )


def downgrade() -> None:
    op.drop_table("ame_meta")

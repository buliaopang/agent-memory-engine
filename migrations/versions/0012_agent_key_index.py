"""Index agents.credential_hash (Phase 4a, L5). Incremental; 0001-0011 untouched.

Agent-key authentication looks an agent up by sha256(key) on every request; with many
registered agents that was a full scan. Pure index: no data change, fully reversible.

Revision ID: 0012_agent_key_index
Revises: 0011_idempotency
"""

from __future__ import annotations

from alembic import op

revision = "0012_agent_key_index"
down_revision = "0011_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_agents_credential_hash ON agents (credential_hash)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agents_credential_hash")

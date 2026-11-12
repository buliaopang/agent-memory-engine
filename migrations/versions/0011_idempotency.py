"""Idempotency records (Phase 3b, ADR-027). Incremental; 0001-0010 untouched.

idempotency_keys holds one row per (actor_id, idem_key): the request fingerprint and, once
the request finished, the stored HTTP status + response body. Rows expire (default 24 h) and
are purged; they are a replay cache, not a record of truth (audit_events is untouched).
Downgrade drops the table (cache only; no business or audit data is lost).

Revision ID: 0011_idempotency
Revises: 0010_space_acl
"""

from __future__ import annotations

from alembic import op

revision = "0011_idempotency"
down_revision = "0010_space_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE idempotency_keys ("
        " actor_id TEXT NOT NULL,"
        " idem_key TEXT NOT NULL,"
        " request_hash TEXT NOT NULL,"
        " state TEXT NOT NULL,"
        " status_code INTEGER,"
        " response_body TEXT,"
        " created_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL,"
        " PRIMARY KEY (actor_id, idem_key),"
        " CONSTRAINT ck_idempotency_state CHECK (state IN ('pending', 'done')),"
        " CONSTRAINT ck_idempotency_done CHECK ("
        "  (state = 'pending' AND status_code IS NULL AND response_body IS NULL) OR"
        "  (state = 'done' AND status_code IS NOT NULL AND response_body IS NOT NULL))"
        ")"
    )
    op.execute("CREATE INDEX ix_idempotency_keys_expires_at ON idempotency_keys (expires_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_idempotency_keys_expires_at")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")

"""Space access lists + agent credential bookkeeping (Phase 3a, ADR-024/026). Incremental;
0001-0009 untouched.

- space_members (from 0006) gains granted_at/granted_by/revoked_at/revoked_by. A grant is
  never deleted: revoking sets revoked_at/revoked_by (trigger forbids DELETE). role keeps
  its 0006 meaning: reader = read; editor/owner = read + write.
- agents gains credential_issued_at and disabled_at (credential_hash already exists).
- Back-fill: pre-existing member rows get granted_at = migration time, granted_by = 'migration'.
- Shared spaces need no rows: every ENABLED agent may read/write them (decision 4, in code).
Downgrade drops the added columns and the trigger (no grant rows are deleted).

Revision ID: 0010_space_acl
Revises: 0009_audit_registry
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0010_space_acl"
down_revision = "0009_audit_registry"
branch_labels = None
depends_on = None

T = sa.Text
MEMBER_COLS = ("granted_at", "granted_by", "revoked_at", "revoked_by")
AGENT_COLS = ("credential_issued_at", "disabled_at")
TRIGGERS = {
    "trg_space_members_no_delete": (
        "CREATE TRIGGER trg_space_members_no_delete BEFORE DELETE ON space_members "
        "BEGIN SELECT RAISE(ABORT, 'space_members: delete not allowed; revoke instead'); END"
    ),
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def record_fingerprints(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
    ).fetchall()
    fp = {n: hashlib.sha256(" ".join(s.split()).encode()).hexdigest() for n, s in rows}
    bind.execute(
        sa.text(
            "INSERT INTO ame_meta(key, value, updated_at) VALUES ('trigger_fingerprints', :v, :t) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at"
        ),
        {"v": json.dumps(fp, sort_keys=True), "t": _now()},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for c in MEMBER_COLS:
        op.execute(f"ALTER TABLE space_members ADD COLUMN {c} TEXT")
    for c in AGENT_COLS:
        op.execute(f"ALTER TABLE agents ADD COLUMN {c} TEXT")
    bind.execute(
        sa.text(
            "UPDATE space_members SET granted_at = :t, granted_by = 'migration' "
            "WHERE granted_at IS NULL"
        ),
        {"t": _now()},
    )
    for sql in TRIGGERS.values():
        op.execute(sql)
    record_fingerprints(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for c in MEMBER_COLS:
        op.execute(f"ALTER TABLE space_members DROP COLUMN {c}")
    for c in AGENT_COLS:
        op.execute(f"ALTER TABLE agents DROP COLUMN {c}")
    record_fingerprints(bind)

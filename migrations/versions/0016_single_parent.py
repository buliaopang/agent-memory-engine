"""Single PARENT per memory: the PARENT edges form a parent tree (ADR-033, Conflux).

Incremental; 0001-0015 untouched.

- BEFORE INSERT trigger: a memory may have at most ONE active incoming PARENT edge
  (PARENT direction is parent -> child, so the child is ``to_memory_id``). Revoked PARENT
  edges never count, so re-parenting = revoke the old edge, then add the new one.
  UPDATE needs no extra guard: 0003 only allows active->revoked updates.
- Existing data is NOT changed (never auto-repair). A database that already has memories
  with several active PARENT edges migrates fine; those memories are reported by
  ``ame check-graph`` G017 (warning) and the consensus view uses the oldest PARENT edge
  as the tree parent until the user revokes the extra edges.
- Partial index on active PARENT edges by child for the trigger and the consensus reads.

Revision ID: 0016_single_parent
Revises: 0015_distill_jobs
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0016_single_parent"
down_revision = "0015_distill_jobs"
branch_labels = None
depends_on = None

TRIGGER_NAME = "trg_memory_edges_single_parent"
TRIGGER_SQL = (
    "CREATE TRIGGER trg_memory_edges_single_parent BEFORE INSERT ON memory_edges "
    "WHEN NEW.edge_type = 'PARENT' AND NEW.status = 'active' AND EXISTS ("
    "SELECT 1 FROM memory_edges e WHERE e.to_memory_id = NEW.to_memory_id "
    "AND e.edge_type = 'PARENT' AND e.status = 'active') "
    "BEGIN SELECT RAISE(ABORT, 'PARENT_ALREADY_EXISTS: memory already has an active PARENT "
    "edge'); END"
)
INDEX_NAME = "ix_memory_edges_active_parent_child"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def record_fingerprints(bind: sa.Connection) -> None:  # same as 0009 (copied: 0009 is frozen)
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
    bind.execute(
        sa.text(
            f"CREATE INDEX {INDEX_NAME} ON memory_edges (to_memory_id) "
            "WHERE status = 'active' AND edge_type = 'PARENT'"
        )
    )
    bind.execute(sa.text(TRIGGER_SQL))
    record_fingerprints(bind)


def downgrade() -> None:
    # Structural rollback only (no data is transformed).
    bind = op.get_bind()
    bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME}"))
    bind.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
    record_fingerprints(bind)

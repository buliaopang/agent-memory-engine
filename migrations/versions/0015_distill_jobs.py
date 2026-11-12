"""Distill jobs: record of every manual, rule-based distill draft (Phase 4e, ADR-032).
Incremental; 0001-0014 untouched.

distill_jobs is append-only (triggers forbid UPDATE/DELETE). Each row = one draft memory
(always created as candidate) built from input_ids_json by a deterministic rule.
Downgrade refuses while any row exists.

Revision ID: 0015_distill_jobs
Revises: 0014_epochs
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0015_distill_jobs"
down_revision = "0014_epochs"
branch_labels = None
depends_on = None

TRIGGERS = (
    "CREATE TRIGGER trg_distill_jobs_no_update BEFORE UPDATE ON distill_jobs "
    "BEGIN SELECT RAISE(ABORT, 'distill_jobs is append-only'); END",
    "CREATE TRIGGER trg_distill_jobs_no_delete BEFORE DELETE ON distill_jobs "
    "BEGIN SELECT RAISE(ABORT, 'distill_jobs is append-only'); END",
)


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
            "CREATE TABLE distill_jobs ("
            " job_id TEXT NOT NULL,"
            " rule TEXT NOT NULL,"
            " params_json TEXT NOT NULL,"
            " input_ids_json TEXT NOT NULL,"
            " input_key TEXT NOT NULL,"
            " output_memory_id TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " actor TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " PRIMARY KEY (job_id),"
            " FOREIGN KEY(output_memory_id) REFERENCES memories (memory_id),"
            " CONSTRAINT ck_distill_jobs_status CHECK (status IN ('created')),"
            " CONSTRAINT uq_distill_jobs_input UNIQUE (rule, input_key)"
            ")"
        )
    )
    for sql in TRIGGERS:
        bind.execute(sa.text(sql))
    record_fingerprints(bind)


def downgrade() -> None:
    bind = op.get_bind()
    n = bind.execute(sa.text("SELECT COUNT(*) FROM distill_jobs")).scalar_one()
    if n:
        raise RuntimeError(
            f"0015 downgrade refused: {n} distill jobs exist; data is never dropped. "
            "Restore the pre-migration backup instead (`ame restore`)."
        )
    bind.execute(sa.text("DROP TABLE distill_jobs"))
    record_fingerprints(bind)

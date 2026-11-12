"""Epochs: user-defined time/topic periods (Phase 4c, ADR-030). Incremental; 0001-0013 untouched.

epochs(status open|closed; closed is final) and epoch_members (soft removal via removed_at;
no DELETE anywhere). Triggers: no delete on either table, a closed epoch cannot be reopened,
and members of a closed epoch cannot be added, re-added or removed.
Downgrade refuses while any epoch row exists (no data is ever dropped).

Revision ID: 0014_epochs
Revises: 0013_feedback
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0014_epochs"
down_revision = "0013_feedback"
branch_labels = None
depends_on = None

TRIGGERS = (
    "CREATE TRIGGER trg_epochs_no_delete BEFORE DELETE ON epochs "
    "BEGIN SELECT RAISE(ABORT, 'epochs cannot be deleted'); END",
    "CREATE TRIGGER trg_epochs_closed_final BEFORE UPDATE ON epochs "
    "WHEN OLD.status = 'closed' "
    "BEGIN SELECT RAISE(ABORT, 'EPOCH_CLOSED: a closed epoch is final'); END",
    "CREATE TRIGGER trg_epoch_members_no_delete BEFORE DELETE ON epoch_members "
    "BEGIN SELECT RAISE(ABORT, 'epoch_members cannot be deleted'); END",
    "CREATE TRIGGER trg_epoch_members_closed_insert BEFORE INSERT ON epoch_members "
    "WHEN (SELECT status FROM epochs WHERE epoch_id = NEW.epoch_id) IS NOT 'open' "
    "BEGIN SELECT RAISE(ABORT, 'EPOCH_CLOSED: epoch is closed or missing'); END",
    "CREATE TRIGGER trg_epoch_members_closed_update BEFORE UPDATE ON epoch_members "
    "WHEN (SELECT status FROM epochs WHERE epoch_id = OLD.epoch_id) IS NOT 'open' "
    "BEGIN SELECT RAISE(ABORT, 'EPOCH_CLOSED: epoch is closed or missing'); END",
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
            "CREATE TABLE epochs ("
            " epoch_id TEXT NOT NULL,"
            " name TEXT NOT NULL,"
            " space_id TEXT,"
            " start_at TEXT NOT NULL,"
            " end_at TEXT,"
            " status TEXT NOT NULL,"
            " created_by TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " metadata_json TEXT NOT NULL,"
            " PRIMARY KEY (epoch_id),"
            " FOREIGN KEY(space_id) REFERENCES spaces (space_id),"
            " CONSTRAINT ck_epochs_status CHECK (status IN ('open', 'closed')),"
            " CONSTRAINT ck_epochs_closed_end CHECK ("
            "  (status = 'open' AND end_at IS NULL) OR (status = 'closed' AND end_at IS NOT NULL))"
            ")"
        )
    )
    bind.execute(
        sa.text(
            "CREATE TABLE epoch_members ("
            " epoch_id TEXT NOT NULL,"
            " memory_id TEXT NOT NULL,"
            " added_at TEXT NOT NULL,"
            " added_by TEXT NOT NULL,"
            " removed_at TEXT,"
            " removed_by TEXT,"
            " PRIMARY KEY (epoch_id, memory_id),"
            " FOREIGN KEY(epoch_id) REFERENCES epochs (epoch_id),"
            " FOREIGN KEY(memory_id) REFERENCES memories (memory_id),"
            " CONSTRAINT ck_epoch_members_removed CHECK ("
            "  (removed_at IS NULL AND removed_by IS NULL) OR"
            "  (removed_at IS NOT NULL AND removed_by IS NOT NULL))"
            ")"
        )
    )
    bind.execute(sa.text("CREATE INDEX ix_epoch_members_memory ON epoch_members (memory_id)"))
    for sql in TRIGGERS:
        bind.execute(sa.text(sql))
    record_fingerprints(bind)


def downgrade() -> None:
    bind = op.get_bind()
    n = bind.execute(sa.text("SELECT COUNT(*) FROM epochs")).scalar_one()
    if n:
        raise RuntimeError(
            f"0014 downgrade refused: {n} epochs exist; data is never dropped. "
            "Restore the pre-migration backup instead (`ame restore`)."
        )
    bind.execute(sa.text("DROP TABLE epoch_members"))
    bind.execute(sa.text("DROP TABLE epochs"))
    record_fingerprints(bind)

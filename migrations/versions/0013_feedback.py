"""Memory feedback + audit types for Phase 4 (ADR-029..032, DECISIONS C55+).
Incremental; 0001-0012 untouched.

1. memory_feedback: append-only (triggers forbid UPDATE/DELETE). One row per submission;
   only the latest row per (memory, actor) counts in scoring (anti-spam, ADR-029).
2. audit_events is rebuilt exactly like 0009 (SQLite cannot ALTER a CHECK): copy every row,
   verify row count + sha256 over every column of every row, then swap. New event types:
   FEEDBACK_ADDED, MEMORY_PROMOTED, MEMORY_REJECTED, DISTILL_CREATED, EPOCH_CREATED, EPOCH_CLOSED,
   EPOCH_MEMBER_ADDED, EPOCH_MEMBER_REMOVED. New entity types: feedback, epoch, distill.
Downgrade refuses while any feedback row or new-type audit row exists (history is never
dropped; restore the pre-migration backup instead).
The rebuild helpers are copied from 0009 on purpose: executed migrations are frozen and
must not be imported or edited.

Revision ID: 0013_feedback
Revises: 0012_agent_key_index
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0013_feedback"
down_revision = "0012_agent_key_index"
branch_labels = None
depends_on = None

OLD_EVENTS = (
    "MEMORY_CREATED", "MEMORY_UPDATED", "MEMORY_VERSION_CREATED", "MEMORY_STATUS_CHANGED",
    "MEMORY_RESTORED", "EDGE_CREATED", "EDGE_REVOKED", "RELATION_CREATED", "RELATION_REVOKED",
    "CONFLICT_OPENED", "CONFLICT_RESOLVED",
    "SPACE_CREATED", "AGENT_REGISTERED", "AGENT_DISABLED", "AGENT_ENABLED",
    "AGENT_CREDENTIAL_ISSUED", "SPACE_MEMBER_GRANTED", "SPACE_MEMBER_REVOKED",
)  # fmt: skip
NEW_EVENTS = (
    "FEEDBACK_ADDED", "MEMORY_PROMOTED", "MEMORY_REJECTED", "DISTILL_CREATED",
    "EPOCH_CREATED", "EPOCH_CLOSED", "EPOCH_MEMBER_ADDED", "EPOCH_MEMBER_REMOVED",
)  # fmt: skip
OLD_ENTITIES = ("memory", "version", "edge", "relation", "conflict", "space", "agent")
NEW_ENTITIES = ("feedback", "epoch", "distill")
FEEDBACK_KINDS = ("useful", "not_useful", "outdated", "wrong")
COLS = (
    "event_id", "seq", "event_type", "entity_type", "entity_id", "memory_id",
    "related_memory_id", "actor_type", "actor_id", "occurred_at", "reason", "before_json",
    "after_json", "correlation_id",
)  # fmt: skip
INDEXES = (
    ("ix_audit_events_memory", ("memory_id", "seq")),
    ("ix_audit_events_related", ("related_memory_id", "seq")),
    ("ix_audit_events_entity", ("entity_type", "entity_id")),
    ("ix_audit_events_correlation", ("correlation_id",)),
)
TRIGGERS = {  # identical to 0005 / 0009
    "trg_audit_events_no_update": (
        "CREATE TRIGGER trg_audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    ),
    "trg_audit_events_no_delete": (
        "CREATE TRIGGER trg_audit_events_no_delete BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    ),
}
FEEDBACK_TRIGGERS = (
    "CREATE TRIGGER trg_memory_feedback_no_update BEFORE UPDATE ON memory_feedback "
    "BEGIN SELECT RAISE(ABORT, 'memory_feedback is append-only'); END",
    "CREATE TRIGGER trg_memory_feedback_no_delete BEFORE DELETE ON memory_feedback "
    "BEGIN SELECT RAISE(ABORT, 'memory_feedback is append-only'); END",
)


def _in(col: str, vals: tuple[str, ...]) -> str:
    return f"{col} IN (" + ", ".join(f"'{v}'" for v in vals) + ")"


def _create_sql(name: str, events: tuple[str, ...], entities: tuple[str, ...]) -> str:
    return (
        f"CREATE TABLE {name} (event_id TEXT NOT NULL, seq INTEGER NOT NULL, "
        "event_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, "
        "memory_id TEXT, related_memory_id TEXT, actor_type TEXT NOT NULL, "
        "actor_id TEXT NOT NULL, occurred_at TEXT NOT NULL, reason TEXT NOT NULL, "
        "before_json TEXT, after_json TEXT, correlation_id TEXT NOT NULL, "
        "PRIMARY KEY (event_id), CONSTRAINT uq_audit_events_seq UNIQUE (seq), "
        f"CONSTRAINT ck_audit_events_event_type CHECK ({_in('event_type', events)}), "
        f"CONSTRAINT ck_audit_events_entity_type CHECK ({_in('entity_type', entities)}), "
        "CONSTRAINT ck_audit_events_actor_type CHECK (actor_type IN ('user', 'agent', 'system')))"
    )


def fingerprint(bind: sa.Connection, table: str) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    cols = ", ".join(COLS)
    for row in bind.execute(sa.text(f"SELECT {cols} FROM {table} ORDER BY seq")):  # noqa: S608
        h.update(json.dumps(list(row), ensure_ascii=False).encode())
        h.update(b"\n")
        n += 1
    return n, h.hexdigest()


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


def rebuild(bind: sa.Connection, events: tuple[str, ...], entities: tuple[str, ...]) -> None:
    before = fingerprint(bind, "audit_events")
    bind.execute(sa.text("DROP TABLE IF EXISTS audit_events_new"))
    bind.execute(sa.text(_create_sql("audit_events_new", events, entities)))
    cols = ", ".join(COLS)
    bind.execute(
        sa.text(
            f"INSERT INTO audit_events_new ({cols}) SELECT {cols} FROM audit_events "  # noqa: S608
            "ORDER BY seq"
        )
    )
    copied = fingerprint(bind, "audit_events_new")
    if copied != before:
        raise RuntimeError(
            f"audit_events copy verification failed: before={before[0]} rows, "
            f"copied={copied[0]} rows (fingerprints differ)"
        )
    bind.execute(sa.text("DROP TABLE audit_events"))
    bind.execute(sa.text("ALTER TABLE audit_events_new RENAME TO audit_events"))
    for name, idx_cols in INDEXES:
        bind.execute(sa.text(f"CREATE INDEX {name} ON audit_events ({', '.join(idx_cols)})"))
    for sql in TRIGGERS.values():
        bind.execute(sa.text(sql))
    after = fingerprint(bind, "audit_events")
    if after != before:
        raise RuntimeError("audit_events verification after rename failed")


def upgrade() -> None:
    bind = op.get_bind()
    rebuild(bind, OLD_EVENTS + NEW_EVENTS, OLD_ENTITIES + NEW_ENTITIES)
    bind.execute(
        sa.text(
            "CREATE TABLE memory_feedback ("
            " feedback_id TEXT NOT NULL,"
            " memory_id TEXT NOT NULL,"
            " actor_type TEXT NOT NULL,"
            " actor_id TEXT NOT NULL,"
            " feedback TEXT NOT NULL,"
            " reason TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " PRIMARY KEY (feedback_id),"
            " FOREIGN KEY(memory_id) REFERENCES memories (memory_id),"
            f" CONSTRAINT ck_memory_feedback_kind CHECK ({_in('feedback', FEEDBACK_KINDS)}),"
            " CONSTRAINT ck_memory_feedback_actor_type"
            "  CHECK (actor_type IN ('user', 'agent', 'system'))"
            ")"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX ix_memory_feedback_memory_actor "
            "ON memory_feedback (memory_id, actor_id, created_at)"
        )
    )
    for sql in FEEDBACK_TRIGGERS:
        bind.execute(sa.text(sql))
    record_fingerprints(bind)


def downgrade() -> None:
    bind = op.get_bind()
    fb = bind.execute(sa.text("SELECT COUNT(*) FROM memory_feedback")).scalar_one()
    n = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM audit_events WHERE NOT ({_in('event_type', OLD_EVENTS)}) "  # noqa: S608
            f"OR NOT ({_in('entity_type', OLD_ENTITIES)})"
        )
    ).scalar_one()
    if fb or n:
        raise RuntimeError(
            f"0013 downgrade refused: {fb} feedback rows / {n} audit rows use Phase 4 types; "
            "history is never dropped. Restore the pre-migration backup instead (`ame restore`)."
        )
    bind.execute(sa.text("DROP TABLE memory_feedback"))
    rebuild(bind, OLD_EVENTS, OLD_ENTITIES)
    record_fingerprints(bind)

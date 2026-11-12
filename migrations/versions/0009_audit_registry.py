"""Expand audit_events for registry/ACL events (Phase 3a, ADR-025, DECISIONS C28+).
Incremental; 0001-0008 untouched.

SQLite cannot ALTER a CHECK constraint, so audit_events is rebuilt:
  1. create audit_events_new with the widened CHECKs (all other columns/constraints equal)
  2. copy every row (ORDER BY seq)
  3. verify: same row count AND same sha256 fingerprint over every column of every row
     (computed in Python from both tables); any mismatch aborts -> the migration runner
     restores the automatic pre-migration backup
  4. drop the old table (its append-only triggers go with it), rename the copy,
     recreate the 4 indexes and the 2 append-only triggers with the 0005 SQL
  5. re-record trigger fingerprints (G007)
New event types: SPACE_CREATED, AGENT_REGISTERED, AGENT_DISABLED, AGENT_ENABLED,
AGENT_CREDENTIAL_ISSUED, SPACE_MEMBER_GRANTED, SPACE_MEMBER_REVOKED. New entity types:
space, agent. Downgrade rebuilds with the old CHECKs only if no new-type rows exist.

Revision ID: 0009_audit_registry
Revises: 0008_cjk_compact_index
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0009_audit_registry"
down_revision = "0008_cjk_compact_index"
branch_labels = None
depends_on = None

OLD_EVENTS = (
    "MEMORY_CREATED", "MEMORY_UPDATED", "MEMORY_VERSION_CREATED", "MEMORY_STATUS_CHANGED",
    "MEMORY_RESTORED", "EDGE_CREATED", "EDGE_REVOKED", "RELATION_CREATED", "RELATION_REVOKED",
    "CONFLICT_OPENED", "CONFLICT_RESOLVED",
)  # fmt: skip
NEW_EVENTS = (
    "SPACE_CREATED", "AGENT_REGISTERED", "AGENT_DISABLED", "AGENT_ENABLED",
    "AGENT_CREDENTIAL_ISSUED", "SPACE_MEMBER_GRANTED", "SPACE_MEMBER_REVOKED",
)  # fmt: skip
OLD_ENTITIES = ("memory", "version", "edge", "relation", "conflict")
NEW_ENTITIES = ("space", "agent")
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
TRIGGERS = {  # identical to 0005
    "trg_audit_events_no_update": (
        "CREATE TRIGGER trg_audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    ),
    "trg_audit_events_no_delete": (
        "CREATE TRIGGER trg_audit_events_no_delete BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    ),
}


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
    """(row count, sha256 over every column of every row ordered by seq)."""
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
    bind.execute(sa.text("DROP TABLE audit_events"))  # drops its indexes + triggers
    bind.execute(sa.text("ALTER TABLE audit_events_new RENAME TO audit_events"))
    for name, idx_cols in INDEXES:
        bind.execute(sa.text(f"CREATE INDEX {name} ON audit_events ({', '.join(idx_cols)})"))
    for sql in TRIGGERS.values():
        bind.execute(sa.text(sql))
    after = fingerprint(bind, "audit_events")
    if after != before:
        raise RuntimeError("audit_events verification after rename failed")
    record_fingerprints(bind)


def upgrade() -> None:
    rebuild(op.get_bind(), OLD_EVENTS + NEW_EVENTS, OLD_ENTITIES + NEW_ENTITIES)


def downgrade() -> None:
    bind = op.get_bind()
    n = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM audit_events WHERE NOT ({_in('event_type', OLD_EVENTS)}) "  # noqa: S608
            f"OR NOT ({_in('entity_type', OLD_ENTITIES)})"
        )
    ).scalar_one()
    if n:
        raise RuntimeError(
            f"0009 downgrade refused: {n} audit rows use Phase 3a event types; audit history "
            "is never dropped. Restore the pre-migration backup instead (`ame restore`)."
        )
    rebuild(bind, OLD_EVENTS, OLD_ENTITIES)

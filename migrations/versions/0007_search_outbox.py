"""Lexical search index, outbox and vector index state (Phase 2, ADR-020/021). Incremental;
0001-0006 untouched.

- memory_fts: FTS5 virtual table (trigram tokenizer, case-insensitive) holding a copy of the
  CURRENT version's content/summary plus tags. Derived data inside SQLite; it can always be
  rebuilt from memories/memory_versions/memory_tags (`ame index rebuild-lexical`).
  Kept in sync by triggers (new current version, tag insert/delete), so direct writes are
  covered too.
- outbox_events (DATA_MODEL.md columns): every new current version and every status change
  enqueues an event IN THE SAME TRANSACTION (triggers). Payload holds ids only, never text.
  Outbox rows cannot be deleted.
- vector_index_state: per-memory state of the derived vector index (no text, no vectors).
- Back-fill: FTS rows for all existing memories; one 'memory.index' event per memory.
- Records trigger fingerprints for `ame check-graph` (G007). Downgrade raises.

Revision ID: 0007_search_outbox
Revises: 0006_agents_spaces
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0007_search_outbox"
down_revision = "0006_agents_spaces"
branch_labels = None
depends_on = None

T = sa.Text

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_EVENT_ID = "'evt_' || lower(hex(randomblob(16)))"
_TAGS = (
    "(SELECT COALESCE(group_concat(tag, ' '), '') FROM "
    "(SELECT tag FROM memory_tags WHERE memory_id = {m} ORDER BY tag))"
)
_IS_CURRENT = "NEW.version = (SELECT current_version FROM memories WHERE memory_id = NEW.memory_id)"

TRIGGERS = {
    "trg_memory_versions_fts_sync": (
        "CREATE TRIGGER trg_memory_versions_fts_sync AFTER INSERT ON memory_versions "
        f"WHEN {_IS_CURRENT} BEGIN "
        "DELETE FROM memory_fts WHERE memory_id = NEW.memory_id; "
        "INSERT INTO memory_fts (memory_id, version, content, summary, tags) VALUES "
        "(NEW.memory_id, NEW.version, NEW.content, COALESCE(NEW.summary, ''), "
        + _TAGS.format(m="NEW.memory_id")
        + "); END"
    ),
    "trg_memory_tags_fts_insert": (
        "CREATE TRIGGER trg_memory_tags_fts_insert AFTER INSERT ON memory_tags BEGIN "
        "UPDATE memory_fts SET tags = "
        + _TAGS.format(m="NEW.memory_id")
        + " WHERE memory_id = NEW.memory_id; END"
    ),
    "trg_memory_tags_fts_delete": (
        "CREATE TRIGGER trg_memory_tags_fts_delete AFTER DELETE ON memory_tags BEGIN "
        "UPDATE memory_fts SET tags = "
        + _TAGS.format(m="OLD.memory_id")
        + " WHERE memory_id = OLD.memory_id; END"
    ),
    "trg_memory_versions_outbox": (
        "CREATE TRIGGER trg_memory_versions_outbox AFTER INSERT ON memory_versions "
        f"WHEN {_IS_CURRENT} BEGIN "
        "INSERT INTO outbox_events (event_id, event_type, aggregate_id, payload_json, status, "
        "attempts, next_attempt_at, last_error, created_at, processed_at) VALUES "
        f"({_EVENT_ID}, 'memory.index', NEW.memory_id, "
        "json_object('memory_id', NEW.memory_id, 'version', NEW.version), 'pending', 0, NULL, "
        f"NULL, {_NOW}, NULL); END"
    ),
    "trg_memories_status_outbox": (
        "CREATE TRIGGER trg_memories_status_outbox AFTER UPDATE OF status ON memories "
        "WHEN NEW.status <> OLD.status BEGIN "
        "INSERT INTO outbox_events (event_id, event_type, aggregate_id, payload_json, status, "
        "attempts, next_attempt_at, last_error, created_at, processed_at) VALUES "
        f"({_EVENT_ID}, 'memory.status', NEW.memory_id, "
        "json_object('memory_id', NEW.memory_id, 'status', NEW.status), 'pending', 0, NULL, "
        f"NULL, {_NOW}, NULL); END"
    ),
    "trg_outbox_events_no_delete": (
        "CREATE TRIGGER trg_outbox_events_no_delete BEFORE DELETE ON outbox_events "
        "BEGIN SELECT RAISE(ABORT, 'outbox_events: delete not allowed'); END"
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
    op.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "memory_id UNINDEXED, version UNINDEXED, content, summary, tags, "
        "tokenize = 'trigram')"
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", T, primary_key=True),
        sa.Column("event_type", T, nullable=False),
        sa.Column("aggregate_id", T, nullable=False),
        sa.Column("payload_json", T, nullable=False),
        sa.Column("status", T, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", T, nullable=True),
        sa.Column("last_error", T, nullable=True),
        sa.Column("created_at", T, nullable=False),
        sa.Column("processed_at", T, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')", name="ck_outbox_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
    )
    op.create_index("ix_outbox_events_status_next", "outbox_events", ["status", "next_attempt_at"])
    op.create_table(
        "vector_index_state",
        sa.Column("memory_id", T, sa.ForeignKey("memories.memory_id"), primary_key=True),
        sa.Column("status", T, nullable=False),
        sa.Column("indexed_version", sa.Integer, nullable=True),
        sa.Column("embedding_model", T, nullable=True),
        sa.Column("embedding_dimension", sa.Integer, nullable=True),
        sa.Column("content_hash", T, nullable=True),
        sa.Column("indexed_at", T, nullable=True),
        sa.Column("last_error", T, nullable=True),
        sa.Column("updated_at", T, nullable=False),
        sa.CheckConstraint(
            "status IN ('indexed', 'failed', 'removed', 'disabled')",
            name="ck_vector_index_state_status",
        ),
    )
    bind.execute(
        sa.text(
            "INSERT INTO memory_fts (memory_id, version, content, summary, tags) "
            "SELECT m.memory_id, v.version, v.content, COALESCE(v.summary, ''), "
            + _TAGS.format(m="m.memory_id")
            + " FROM memories m JOIN memory_versions v ON v.memory_id = m.memory_id "
            "AND v.version = m.current_version ORDER BY m.memory_id"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO outbox_events (event_id, event_type, aggregate_id, payload_json, "
            "status, attempts, next_attempt_at, last_error, created_at, processed_at) "
            f"SELECT {_EVENT_ID}, 'memory.index', memory_id, json_object('memory_id', "
            "memory_id, 'version', current_version), 'pending', 0, NULL, NULL, "
            f"{_NOW}, NULL FROM memories ORDER BY memory_id"
        )
    )
    for sql in TRIGGERS.values():
        op.execute(sql)
    record_fingerprints(bind)


def downgrade() -> None:
    raise RuntimeError(
        "0007 has no structural downgrade; restore the automatic pre-migration backup "
        "(`ame restore <backup>`)."
    )

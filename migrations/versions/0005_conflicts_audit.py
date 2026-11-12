"""Conflicts, unified audit log, frozen-endpoint rule (ADR-012, ADR-013, ADR-016).

- memory_conflicts: substantive conflicts (open/resolved) with evidence and resolution.
- audit_events: unified append-only audit log; legacy memory_audit_events rows are
  back-filled (event_id = 'legacy_' || audit_id) and the legacy table is FROZEN (no new
  rows; it is never deleted - Q-D1).
- Existing conflicted memories get an open conflict per active CONTRADICTS relation;
  memories with no such relation are left untouched (check-graph G011 warns).
- conflicted status requires conflict evidence (trigger).
- Q-N11: no new active edge/relation may touch an archived/rejected memory (trigger).

Revision ID: 0005_conflicts_audit
Revises: 0004_dag_integrity
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0005_conflicts_audit"
down_revision = "0004_dag_integrity"
branch_labels = None
depends_on = None

T = sa.Text

AUDIT_TYPES = (
    "MEMORY_CREATED", "MEMORY_UPDATED", "MEMORY_VERSION_CREATED", "MEMORY_STATUS_CHANGED",
    "MEMORY_RESTORED", "EDGE_CREATED", "EDGE_REVOKED", "RELATION_CREATED", "RELATION_REVOKED",
    "CONFLICT_OPENED", "CONFLICT_RESOLVED",
)  # fmt: skip
RESOLUTIONS = ("kept_both", "a_superseded_by_b", "b_superseded_by_a", "dismissed",
               "relation_revoked")  # fmt: skip
RESOLVE_OK = (
    "(status = 'open' AND resolved_at IS NULL AND resolved_by IS NULL "
    "AND resolved_actor_type IS NULL AND resolution IS NULL AND resolution_reason IS NULL "
    "AND resolution_evidence_json IS NULL) OR "
    "(status = 'resolved' AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL "
    "AND resolved_actor_type IS NOT NULL AND resolution IS NOT NULL "
    "AND resolution_reason IS NOT NULL AND resolution_evidence_json IS NOT NULL)"
)


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


_SAME_CONFLICT = " AND ".join(
    f"NEW.{c} = OLD.{c}"
    for c in ("conflict_id", "relation_id", "memory_a_id", "memory_b_id", "opened_at",
              "opened_by", "opened_actor_type", "evidence_json")
)  # fmt: skip

_FROZEN = (
    "NEW.status = 'active' AND EXISTS (SELECT 1 FROM memories WHERE memory_id IN "
    "(NEW.from_memory_id, NEW.to_memory_id) AND status IN ('archived', 'rejected'))"
)

TRIGGERS = {
    "trg_audit_events_no_update": (
        "CREATE TRIGGER trg_audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    ),
    "trg_audit_events_no_delete": (
        "CREATE TRIGGER trg_audit_events_no_delete BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    ),
    "trg_memory_audit_events_frozen": (
        "CREATE TRIGGER trg_memory_audit_events_frozen BEFORE INSERT ON memory_audit_events "
        "BEGIN SELECT RAISE(ABORT, 'memory_audit_events is frozen (read-only legacy); "
        "write audit_events'); END"
    ),
    "trg_memory_conflicts_no_delete": (
        "CREATE TRIGGER trg_memory_conflicts_no_delete BEFORE DELETE ON memory_conflicts "
        "BEGIN SELECT RAISE(ABORT, 'memory_conflicts: delete not allowed'); END"
    ),
    "trg_memory_conflicts_resolve_only": (
        "CREATE TRIGGER trg_memory_conflicts_resolve_only BEFORE UPDATE ON memory_conflicts "
        f"WHEN NOT (OLD.status = 'open' AND NEW.status = 'resolved' AND {_SAME_CONFLICT}) "
        "BEGIN SELECT RAISE(ABORT, 'memory_conflicts: only open->resolved updates are allowed'); "
        "END"
    ),
    "trg_memory_conflicts_open_insert": (
        "CREATE TRIGGER trg_memory_conflicts_open_insert BEFORE INSERT ON memory_conflicts "
        "WHEN NEW.status <> 'open' OR NOT EXISTS (SELECT 1 FROM memory_relations r "
        "WHERE r.relation_id = NEW.relation_id AND r.relation_type = 'CONTRADICTS' "
        "AND r.status = 'active' AND r.from_memory_id = NEW.memory_a_id "
        "AND r.to_memory_id = NEW.memory_b_id) "
        "BEGIN SELECT RAISE(ABORT, 'CONFLICT_EVIDENCE_REQUIRED: a conflict must be opened on "
        "an active CONTRADICTS relation between the same memories'); END"
    ),
    "trg_memories_conflicted_requires_evidence": (
        "CREATE TRIGGER trg_memories_conflicted_requires_evidence BEFORE UPDATE OF status "
        "ON memories WHEN NEW.status = 'conflicted' AND OLD.status <> 'conflicted' "
        "AND NOT EXISTS (SELECT 1 FROM memory_conflicts c WHERE NEW.memory_id IN "
        "(c.memory_a_id, c.memory_b_id) AND (c.status = 'open' OR OLD.status = 'archived')) "
        "BEGIN SELECT RAISE(ABORT, 'CONFLICT_EVIDENCE_REQUIRED: conflicted needs an open "
        "conflict record'); END"
    ),
    "trg_memories_conflicted_insert_guard": (
        "CREATE TRIGGER trg_memories_conflicted_insert_guard BEFORE INSERT ON memories "
        "WHEN NEW.status = 'conflicted' "
        "BEGIN SELECT RAISE(ABORT, 'CONFLICT_EVIDENCE_REQUIRED: a memory cannot be created "
        "as conflicted'); END"
    ),
    "trg_memory_edges_frozen_endpoint": (
        "CREATE TRIGGER trg_memory_edges_frozen_endpoint BEFORE INSERT ON memory_edges "
        f"WHEN {_FROZEN} BEGIN SELECT RAISE(ABORT, 'LINK_ENDPOINT_FROZEN: archived/rejected "
        "memory cannot get new links'); END"
    ),
    "trg_memory_relations_frozen_endpoint": (
        "CREATE TRIGGER trg_memory_relations_frozen_endpoint BEFORE INSERT ON memory_relations "
        f"WHEN {_FROZEN} BEGIN SELECT RAISE(ABORT, 'LINK_ENDPOINT_FROZEN: archived/rejected "
        "memory cannot get new links'); END"
    ),
}

# legacy (0002) -> unified event types
_LEGACY_MAP = (
    "CASE WHEN event_type = 'created' THEN 'MEMORY_CREATED' "
    "WHEN event_type = 'classification_changed' THEN 'MEMORY_UPDATED' "
    "WHEN event_type = 'version_created' THEN 'MEMORY_VERSION_CREATED' "
    "WHEN event_type = 'status_changed' AND \"trigger\" = 'T10' THEN 'MEMORY_RESTORED' "
    "ELSE 'MEMORY_STATUS_CHANGED' END"
)
_LEGACY_BEFORE = (
    "CASE WHEN event_type = 'classification_changed' THEN from_value "
    "WHEN event_type = 'version_created' THEN json_object('version', CAST(from_value AS INTEGER)) "
    "WHEN from_value IS NULL THEN NULL ELSE json_object('status', from_value) END"
)
_LEGACY_AFTER = (
    "CASE WHEN event_type = 'classification_changed' "
    "THEN json_set(to_value, '$.legacy', json('true'), '$.trigger', \"trigger\") "
    "WHEN event_type = 'version_created' THEN json_object('version', CAST(to_value AS INTEGER), "
    "'legacy', json('true'), 'trigger', \"trigger\") "
    "ELSE json_object('status', to_value, 'trigger', \"trigger\", 'legacy', json('true')) END"
)


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
    op.create_table(
        "memory_conflicts",
        sa.Column("conflict_id", T, primary_key=True),
        sa.Column(
            "relation_id", T, sa.ForeignKey("memory_relations.relation_id"), nullable=False
        ),  # fmt: skip
        sa.Column("memory_a_id", T, sa.ForeignKey("memories.memory_id"), nullable=False),
        sa.Column("memory_b_id", T, sa.ForeignKey("memories.memory_id"), nullable=False),
        sa.Column("status", T, nullable=False),
        sa.Column("opened_at", T, nullable=False),
        sa.Column("opened_by", T, nullable=False),
        sa.Column("opened_actor_type", T, nullable=False),
        sa.Column("evidence_json", T, nullable=False),
        sa.Column("resolved_at", T, nullable=True),
        sa.Column("resolved_by", T, nullable=True),
        sa.Column("resolved_actor_type", T, nullable=True),
        sa.Column("resolution", T, nullable=True),
        sa.Column("resolution_reason", T, nullable=True),
        sa.Column("resolution_evidence_json", T, nullable=True),
        sa.CheckConstraint("memory_a_id < memory_b_id", name="ck_memory_conflicts_normalized"),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_memory_conflicts_status"),
        sa.CheckConstraint(
            "resolution IS NULL OR " + _in("resolution", RESOLUTIONS),
            name="ck_memory_conflicts_resolution",
        ),
        sa.CheckConstraint(
            "opened_actor_type IN ('user', 'agent', 'system') AND (resolved_actor_type IS NULL "
            "OR resolved_actor_type IN ('user', 'agent', 'system'))",
            name="ck_memory_conflicts_actor_type",
        ),
        sa.CheckConstraint(RESOLVE_OK, name="ck_memory_conflicts_resolve_fields"),
    )
    op.create_index(
        "uq_memory_conflicts_open_relation", "memory_conflicts", ["relation_id"], unique=True,
        sqlite_where=sa.text("status = 'open'"),
    )  # fmt: skip
    op.create_index("ix_memory_conflicts_a", "memory_conflicts", ["memory_a_id", "status"])
    op.create_index("ix_memory_conflicts_b", "memory_conflicts", ["memory_b_id", "status"])

    op.create_table(
        "audit_events",
        sa.Column("event_id", T, primary_key=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("event_type", T, nullable=False),
        sa.Column("entity_type", T, nullable=False),
        sa.Column("entity_id", T, nullable=False),
        sa.Column("memory_id", T, nullable=True),
        sa.Column("related_memory_id", T, nullable=True),
        sa.Column("actor_type", T, nullable=False),
        sa.Column("actor_id", T, nullable=False),
        sa.Column("occurred_at", T, nullable=False),
        sa.Column("reason", T, nullable=False),
        sa.Column("before_json", T, nullable=True),
        sa.Column("after_json", T, nullable=True),
        sa.Column("correlation_id", T, nullable=False),
        sa.UniqueConstraint("seq", name="uq_audit_events_seq"),
        sa.CheckConstraint(_in("event_type", AUDIT_TYPES), name="ck_audit_events_event_type"),
        sa.CheckConstraint(
            "entity_type IN ('memory', 'version', 'edge', 'relation', 'conflict')",
            name="ck_audit_events_entity_type",
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'agent', 'system')", name="ck_audit_events_actor_type"
        ),
    )
    op.create_index("ix_audit_events_memory", "audit_events", ["memory_id", "seq"])
    op.create_index("ix_audit_events_related", "audit_events", ["related_memory_id", "seq"])
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])
    op.create_index("ix_audit_events_correlation", "audit_events", ["correlation_id"])

    # ---- back-fill legacy audit history (legacy table itself is kept unchanged) ----
    op.execute(
        "INSERT INTO audit_events (event_id, seq, event_type, entity_type, entity_id, memory_id, "
        "related_memory_id, actor_type, actor_id, occurred_at, reason, before_json, after_json, "
        "correlation_id) SELECT 'legacy_' || audit_id, "
        "ROW_NUMBER() OVER (ORDER BY created_at, audit_id), "
        f"{_LEGACY_MAP}, 'memory', memory_id, memory_id, NULL, actor_type, actor_id, created_at, "
        f"reason, {_LEGACY_BEFORE}, {_LEGACY_AFTER}, 'legacy_' || audit_id "
        "FROM memory_audit_events"
    )

    # ---- open conflicts for memories that are already conflicted (evidence back-fill) ----
    now = _now()
    rels = bind.execute(
        sa.text(
            "SELECT r.relation_id, r.from_memory_id, r.to_memory_id FROM memory_relations r "
            "JOIN memories a ON a.memory_id = r.from_memory_id "
            "JOIN memories b ON b.memory_id = r.to_memory_id "
            "WHERE r.relation_type = 'CONTRADICTS' AND r.status = 'active' "
            "AND (a.status = 'conflicted' OR b.status = 'conflicted') ORDER BY r.relation_id"
        )
    ).fetchall()
    seq = bind.execute(sa.text("SELECT COALESCE(MAX(seq), 0) FROM audit_events")).scalar_one()
    for rid, a, b in rels:
        seq += 1
        cid = "cfl_backfill_" + rid
        ev = json.dumps({"relation_id": rid, "backfilled_by_migration": "0005"})
        bind.execute(
            sa.text(
                "INSERT INTO memory_conflicts (conflict_id, relation_id, memory_a_id, memory_b_id, "
                "status, opened_at, opened_by, opened_actor_type, evidence_json) "
                "VALUES (:c, :r, :a, :b, 'open', :t, 'migration:0005', 'system', :e)"
            ),
            {"c": cid, "r": rid, "a": a, "b": b, "t": now, "e": ev},
        )
        bind.execute(
            sa.text(
                "INSERT INTO audit_events (event_id, seq, event_type, entity_type, entity_id, "
                "memory_id, related_memory_id, actor_type, actor_id, occurred_at, reason, "
                "before_json, after_json, correlation_id) VALUES (:id, :s, 'CONFLICT_OPENED', "
                "'conflict', :c, :a, :b, 'system', 'migration:0005', :t, "
                "'back-filled by migration 0005', NULL, :e, 'migration_0005')"
            ),
            {"id": "evt_backfill_" + rid, "s": seq, "c": cid, "a": a, "b": b, "t": now, "e": ev},
        )

    for sql in TRIGGERS.values():
        op.execute(sql)
    record_fingerprints(bind)


def downgrade() -> None:
    raise RuntimeError(
        "0005 is not structurally reversible (audit history and conflict decisions would be "
        "lost). Restore the automatic pre-migration backup instead: `ame restore <backup>`."
    )

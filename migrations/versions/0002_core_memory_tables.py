"""Core memory tables per ADR-001..005.

Revision ID: 0002_core_memory_tables
Revises: 0001_baseline
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_core_memory_tables"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

T = sa.Text


def _in(col: str, values: list[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _fk() -> sa.ForeignKey:
    return sa.ForeignKey("memories.memory_id")


APPEND_ONLY = ("memory_versions", "memory_audit_events")


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("memory_id", T, primary_key=True),
        sa.Column("memory_type", T, nullable=False),
        sa.Column("layer", T, nullable=False),
        sa.Column("scope_type", T, nullable=False),
        sa.Column("scope_id", T, nullable=False),
        sa.Column("created_by", T, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
        sa.Column("importance", sa.Float, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("status", T, nullable=False),
        sa.Column("current_version", sa.Integer, nullable=False),
        sa.Column("metadata_json", T, nullable=False),
        sa.CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memories_importance"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memories_confidence"),
        sa.CheckConstraint("current_version >= 1", name="ck_memories_current_version"),
        sa.CheckConstraint(
            _in("memory_type", ["working", "episodic", "semantic", "procedural"]),
            name="ck_memories_memory_type",
        ),
        sa.CheckConstraint(_in("layer", ["L1", "L2", "L3", "L4"]), name="ck_memories_layer"),
        sa.CheckConstraint(
            _in("scope_type", ["private", "project", "shared"]), name="ck_memories_scope_type"
        ),
        sa.CheckConstraint(
            _in(
                "status",
                ["candidate", "active", "conflicted", "superseded", "archived", "rejected"],
            ),
            name="ck_memories_status",
        ),
    )
    op.create_index("ix_memories_scope", "memories", ["scope_type", "scope_id", "status", "layer"])
    op.create_index("ix_memories_created", "memories", ["created_by", "created_at"])
    op.create_index("ix_memories_type", "memories", ["memory_type"])

    op.create_table(
        "memory_versions",
        sa.Column("version_id", T, primary_key=True),
        sa.Column("memory_id", T, _fk(), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content", T, nullable=False),
        sa.Column("summary", T, nullable=True),
        sa.Column("content_hash", T, nullable=False),
        sa.Column("created_by", T, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("change_reason", T, nullable=True),
        sa.UniqueConstraint("memory_id", "version", name="uq_memory_versions_memory_version"),
        sa.CheckConstraint("version >= 1", name="ck_memory_versions_version"),
    )

    op.create_table(
        "memory_edges",
        sa.Column("edge_id", T, primary_key=True),
        sa.Column("from_memory_id", T, _fk(), nullable=False),
        sa.Column("to_memory_id", T, _fk(), nullable=False),
        sa.Column("edge_type", T, nullable=False),
        sa.Column("created_by", T, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("metadata_json", T, nullable=False),
        sa.UniqueConstraint(
            "from_memory_id", "to_memory_id", "edge_type", name="uq_memory_edges_triple"
        ),
        sa.CheckConstraint("from_memory_id <> to_memory_id", name="ck_memory_edges_no_self_loop"),
        sa.CheckConstraint(
            _in("edge_type", ["PARENT", "REFERENCE", "SUPPORTS", "SUPERSEDES", "DERIVED_FROM"]),
            name="ck_memory_edges_edge_type",
        ),
    )
    op.create_index("ix_memory_edges_from", "memory_edges", ["from_memory_id", "edge_type"])
    op.create_index("ix_memory_edges_to", "memory_edges", ["to_memory_id", "edge_type"])

    op.create_table(
        "memory_relations",
        sa.Column("relation_id", T, primary_key=True),
        sa.Column("relation_type", T, nullable=False),
        sa.Column("low_memory_id", T, _fk(), nullable=False),
        sa.Column("high_memory_id", T, _fk(), nullable=False),
        sa.Column("created_by", T, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("metadata_json", T, nullable=False),
        sa.UniqueConstraint(
            "relation_type", "low_memory_id", "high_memory_id", name="uq_memory_relations_pair"
        ),
        sa.CheckConstraint("low_memory_id < high_memory_id", name="ck_memory_relations_normalized"),
        sa.CheckConstraint(
            _in("relation_type", ["CONTRADICTS"]), name="ck_memory_relations_relation_type"
        ),
    )
    op.create_index("ix_memory_relations_low", "memory_relations", ["low_memory_id"])
    op.create_index("ix_memory_relations_high", "memory_relations", ["high_memory_id"])

    op.create_table(
        "memory_tags",
        sa.Column("memory_id", T, _fk(), primary_key=True),
        sa.Column("tag", T, primary_key=True),
    )
    op.create_index("ix_memory_tags_tag", "memory_tags", ["tag"])

    op.create_table(
        "memory_sources",
        sa.Column("source_id", T, primary_key=True),
        sa.Column("memory_id", T, _fk(), nullable=False),
        sa.Column("source_type", T, nullable=False),
        sa.Column("source_ref", T, nullable=True),
        sa.Column("excerpt", T, nullable=True),
        sa.Column("content_hash", T, nullable=True),
        sa.Column("created_at", T, nullable=False),
        sa.Column("metadata_json", T, nullable=False),
        sa.CheckConstraint(
            _in("source_type", ["conversation", "file", "task", "manual", "tool"]),
            name="ck_memory_sources_source_type",
        ),
    )
    op.create_index("ix_memory_sources_memory", "memory_sources", ["memory_id"])

    op.create_table(
        "memory_audit_events",
        sa.Column("audit_id", T, primary_key=True),
        sa.Column("memory_id", T, _fk(), nullable=False),
        sa.Column("event_type", T, nullable=False),
        sa.Column("from_value", T, nullable=True),
        sa.Column("to_value", T, nullable=False),
        sa.Column("trigger", T, nullable=False),
        sa.Column("actor_type", T, nullable=False),
        sa.Column("actor_id", T, nullable=False),
        sa.Column("reason", T, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.CheckConstraint(
            _in(
                "event_type",
                ["created", "status_changed", "classification_changed", "version_created"],
            ),
            name="ck_audit_event_type",
        ),
        sa.CheckConstraint(
            _in("actor_type", ["user", "agent", "system"]), name="ck_audit_actor_type"
        ),
    )
    op.create_index(
        "ix_memory_audit_events_memory", "memory_audit_events", ["memory_id", "created_at"]
    )

    for table in APPEND_ONLY:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )


def downgrade() -> None:
    for table in APPEND_ONLY:
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_{action}")
    for t in (
        "memory_audit_events",
        "memory_sources",
        "memory_tags",
        "memory_relations",
        "memory_edges",
        "memory_versions",
        "memories",
    ):
        op.drop_table(t)

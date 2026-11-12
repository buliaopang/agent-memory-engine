"""ADR-006..009: revocable edges/relations, SUPPORTS -> semantic, RELATED_TO, archive guard.

Revision ID: 0003_link_revocation
Revises: 0002_core_memory_tables

Data migration: existing SUPPORTS structural edges are moved into memory_relations
(directed, same endpoints, relation_id = 'rel_' || edge_id). memory_relations
low/high columns are renamed to from/to (ADR-002 naming).
This migration is not reversible without data loss; downgrade raises.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_link_revocation"
down_revision = "0002_core_memory_tables"
branch_labels = None
depends_on = None

T = sa.Text
REVOKE_OK = (
    "(status = 'active' AND revoked_at IS NULL AND revoked_by IS NULL "
    "AND revoked_actor_type IS NULL AND revoked_reason IS NULL) OR "
    "(status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
    "AND revoked_actor_type IS NOT NULL AND revoked_reason IS NOT NULL)"
)


def _fk() -> sa.ForeignKey:
    return sa.ForeignKey("memories.memory_id")


def _link_cols() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("from_memory_id", T, _fk(), nullable=False),
        sa.Column("to_memory_id", T, _fk(), nullable=False),
    ]


def _tail_cols() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("status", T, nullable=False),
        sa.Column("created_by", T, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("metadata_json", T, nullable=False),
        sa.Column("revoked_at", T, nullable=True),
        sa.Column("revoked_by", T, nullable=True),
        sa.Column("revoked_actor_type", T, nullable=True),
        sa.Column("revoked_reason", T, nullable=True),
    ]


def _immutable_link_trigger(table: str, pk: str, type_col: str) -> str:
    same = " AND ".join(
        f"NEW.{c} = OLD.{c}"
        for c in (pk, "from_memory_id", "to_memory_id", type_col, "created_by", "created_at",
                  "metadata_json")
    )  # fmt: skip
    return (
        f"CREATE TRIGGER trg_{table}_revoke_only BEFORE UPDATE ON {table} "
        f"WHEN NOT (OLD.status = 'active' AND NEW.status = 'revoked' AND {same}) "
        f"BEGIN SELECT RAISE(ABORT, '{table}: only active->revoked updates are allowed'); END;"
    )


def upgrade() -> None:
    # ---- memory_edges: add status + revocation, drop SUPPORTS, partial unique ----
    op.create_table(
        "memory_edges_new",
        sa.Column("edge_id", T, primary_key=True),
        *_link_cols(),
        sa.Column("edge_type", T, nullable=False),
        *_tail_cols(),
        sa.CheckConstraint("from_memory_id <> to_memory_id", name="ck_memory_edges_no_self_loop"),
        sa.CheckConstraint(
            "edge_type IN ('PARENT', 'REFERENCE', 'SUPERSEDES', 'DERIVED_FROM')",
            name="ck_memory_edges_edge_type",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_memory_edges_status"),
        sa.CheckConstraint(REVOKE_OK, name="ck_memory_edges_revoke_fields"),
    )
    op.execute(
        "INSERT INTO memory_edges_new (edge_id, from_memory_id, to_memory_id, edge_type, status, "
        "created_by, created_at, metadata_json) SELECT edge_id, from_memory_id, to_memory_id, "
        "edge_type, 'active', created_by, created_at, metadata_json FROM memory_edges "
        "WHERE edge_type <> 'SUPPORTS'"
    )

    # ---- memory_relations: low/high -> from/to, 3 types, status + revocation ----
    op.create_table(
        "memory_relations_new",
        sa.Column("relation_id", T, primary_key=True),
        sa.Column("relation_type", T, nullable=False),
        *_link_cols(),
        *_tail_cols(),
        sa.CheckConstraint("from_memory_id <> to_memory_id", name="ck_memory_relations_no_self"),
        sa.CheckConstraint(
            "relation_type = 'SUPPORTS' OR from_memory_id < to_memory_id",
            name="ck_memory_relations_normalized",
        ),
        sa.CheckConstraint(
            "relation_type IN ('CONTRADICTS', 'SUPPORTS', 'RELATED_TO')",
            name="ck_memory_relations_relation_type",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_memory_relations_status"),
        sa.CheckConstraint(REVOKE_OK, name="ck_memory_relations_revoke_fields"),
    )
    op.execute(
        "INSERT INTO memory_relations_new (relation_id, relation_type, from_memory_id, "
        "to_memory_id, status, created_by, created_at, metadata_json) SELECT relation_id, "
        "relation_type, low_memory_id, high_memory_id, 'active', created_by, created_at, "
        "metadata_json FROM memory_relations"
    )
    op.execute(
        "INSERT INTO memory_relations_new (relation_id, relation_type, from_memory_id, "
        "to_memory_id, status, created_by, created_at, metadata_json) SELECT 'rel_' || edge_id, "
        "'SUPPORTS', from_memory_id, to_memory_id, 'active', created_by, created_at, "
        "metadata_json FROM memory_edges WHERE edge_type = 'SUPPORTS'"
    )

    op.drop_table("memory_edges")
    op.drop_table("memory_relations")
    op.rename_table("memory_edges_new", "memory_edges")
    op.rename_table("memory_relations_new", "memory_relations")

    active = sa.text("status = 'active'")
    op.create_index(
        "uq_memory_edges_active_triple", "memory_edges",
        ["from_memory_id", "to_memory_id", "edge_type"], unique=True, sqlite_where=active,
    )  # fmt: skip
    op.create_index(
        "ix_memory_edges_from", "memory_edges", ["from_memory_id", "edge_type", "status"]
    )
    op.create_index("ix_memory_edges_to", "memory_edges", ["to_memory_id", "edge_type", "status"])
    op.create_index(
        "uq_memory_relations_active_pair", "memory_relations",
        ["relation_type", "from_memory_id", "to_memory_id"], unique=True, sqlite_where=active,
    )  # fmt: skip
    op.create_index("ix_memory_relations_from", "memory_relations", ["from_memory_id", "status"])
    op.create_index("ix_memory_relations_to", "memory_relations", ["to_memory_id", "status"])

    # ---- DB-level guards (cannot be bypassed by skipping the Service layer) ----
    for table, pk, type_col in (
        ("memory_edges", "edge_id", "edge_type"),
        ("memory_relations", "relation_id", "relation_type"),
    ):
        op.execute(_immutable_link_trigger(table, pk, type_col))
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table}: delete not allowed, revoke instead'); END;"
        )
    # ADR-009: archived memories cannot get new versions.
    op.execute(
        "CREATE TRIGGER trg_memory_versions_archived_guard BEFORE INSERT ON memory_versions "
        "WHEN (SELECT status FROM memories WHERE memory_id = NEW.memory_id) = 'archived' "
        "BEGIN SELECT RAISE(ABORT, 'memory is archived: restore before creating a version'); END;"
    )
    op.execute(
        "CREATE TRIGGER trg_memories_archived_version_guard "
        "BEFORE UPDATE OF current_version ON memories "
        "WHEN OLD.status = 'archived' AND NEW.current_version <> OLD.current_version "
        "BEGIN SELECT RAISE(ABORT, 'memory is archived: restore before creating a version'); END;"
    )


def downgrade() -> None:
    raise RuntimeError("0003 is irreversible (SUPPORTS moved, revocation history would be lost)")

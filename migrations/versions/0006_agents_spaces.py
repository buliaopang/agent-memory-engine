"""Agents, spaces and space members (Phase 1.2, ADR-018). Incremental; 0001-0005 untouched.

- agents / spaces / space_members tables (DATA_MODEL.md column names).
- Back-fill: one space per distinct memories.scope_id (scope_type taken from the memories,
  name = scope_id, owner_agent_id NULL, created_at = earliest memory in that scope).
  No memory row is modified. If one scope_id is used with more than one scope_type the
  migration ABORTS (the runner restores the automatic pre-migration backup).
- Seeds the default space 'shared' (scope_type shared) if absent.
- Triggers: a memory's (scope_id, scope_type) must reference an existing space with the same
  scope_type (insert and update); spaces/agents cannot be deleted; space_id/scope_type of a
  space are immutable. ACL (who may use a space) is NOT enforced here - Phase 3.
- Records trigger fingerprints for `ame check-graph` (G007).

Revision ID: 0006_agents_spaces
Revises: 0005_conflicts_audit
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0006_agents_spaces"
down_revision = "0005_conflicts_audit"
branch_labels = None
depends_on = None

T = sa.Text

_SPACE_OK = (
    "EXISTS (SELECT 1 FROM spaces s WHERE s.space_id = NEW.scope_id "
    "AND s.scope_type = NEW.scope_type)"
)

TRIGGERS = {
    "trg_memories_space_insert": (
        "CREATE TRIGGER trg_memories_space_insert BEFORE INSERT ON memories "
        f"WHEN NOT {_SPACE_OK} "
        "BEGIN SELECT RAISE(ABORT, 'SPACE_NOT_FOUND: scope_id/scope_type has no space'); END"
    ),
    "trg_memories_space_update": (
        "CREATE TRIGGER trg_memories_space_update BEFORE UPDATE OF scope_id, scope_type "
        f"ON memories WHEN NOT {_SPACE_OK} "
        "BEGIN SELECT RAISE(ABORT, 'SPACE_NOT_FOUND: scope_id/scope_type has no space'); END"
    ),
    "trg_spaces_no_delete": (
        "CREATE TRIGGER trg_spaces_no_delete BEFORE DELETE ON spaces "
        "BEGIN SELECT RAISE(ABORT, 'spaces: delete not allowed'); END"
    ),
    "trg_spaces_identity_immutable": (
        "CREATE TRIGGER trg_spaces_identity_immutable BEFORE UPDATE OF space_id, scope_type "
        "ON spaces WHEN NEW.space_id <> OLD.space_id OR NEW.scope_type <> OLD.scope_type "
        "BEGIN SELECT RAISE(ABORT, 'spaces: space_id/scope_type immutable'); END"
    ),
    "trg_agents_no_delete": (
        "CREATE TRIGGER trg_agents_no_delete BEFORE DELETE ON agents "
        "BEGIN SELECT RAISE(ABORT, 'agents: delete not allowed'); END"
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
    clash = bind.execute(
        sa.text(
            "SELECT scope_id FROM memories GROUP BY scope_id "
            "HAVING COUNT(DISTINCT scope_type) > 1 ORDER BY scope_id"
        )
    ).fetchall()
    if clash:
        raise RuntimeError(
            "0006 aborted: scope_id used with several scope_types: "
            + ", ".join(r[0] for r in clash[:20])
        )
    op.create_table(
        "agents",
        sa.Column("agent_id", T, primary_key=True),
        sa.Column("display_name", T, nullable=False),
        sa.Column("agent_type", T, nullable=False),
        sa.Column("credential_hash", T, nullable=True),
        sa.Column("enabled", sa.Integer, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_agents_enabled"),
    )
    op.create_table(
        "spaces",
        sa.Column("space_id", T, primary_key=True),
        sa.Column("name", T, nullable=False),
        sa.Column("scope_type", T, nullable=False),
        sa.Column("owner_agent_id", T, sa.ForeignKey("agents.agent_id"), nullable=True),
        sa.Column("created_at", T, nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('private', 'project', 'shared')", name="ck_spaces_scope_type"
        ),
    )
    op.create_table(
        "space_members",
        sa.Column("space_id", T, sa.ForeignKey("spaces.space_id"), primary_key=True),
        sa.Column("agent_id", T, sa.ForeignKey("agents.agent_id"), primary_key=True),
        sa.Column("role", T, nullable=False),
        sa.CheckConstraint("role IN ('owner', 'editor', 'reader')", name="ck_space_members_role"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO spaces (space_id, name, scope_type, owner_agent_id, created_at) "
            "SELECT scope_id, scope_id, MIN(scope_type), NULL, MIN(created_at) "
            "FROM memories GROUP BY scope_id"
        )
    )
    bind.execute(
        sa.text(
            "INSERT OR IGNORE INTO spaces (space_id, name, scope_type, owner_agent_id, "
            "created_at) VALUES ('shared', 'shared', 'shared', NULL, :t)"
        ),
        {"t": _now()},
    )
    for sql in TRIGGERS.values():
        op.execute(sql)
    record_fingerprints(bind)


def downgrade() -> None:
    raise RuntimeError(
        "0006 has no structural downgrade; restore the automatic pre-migration backup "
        "(`ame restore <backup>`)."
    )

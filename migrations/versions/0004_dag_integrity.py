"""Database-level Structural DAG integrity (ADR-010).

- Aborts (before any DDL) if the existing *active* structural edges already contain a cycle
  (Q-A1: never auto-repair data; run `ame check-graph --repair-dry-run`).
- BEFORE INSERT / BEFORE UPDATE triggers reject edges that would close a cycle. Only
  status='active' structural edges count; revoked edges and memory_relations never do.
  The UPDATE trigger excludes the row being updated (OLD.edge_id).
- Endpoint-existence triggers so dangling links are rejected even on connections that
  have PRAGMA foreign_keys=OFF (Q-A3).
- Partial adjacency index on active edges.
- Records trigger fingerprints in ame_meta for `ame check-graph` (G007).

Revision ID: 0004_dag_integrity
Revises: 0003_link_revocation
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0004_dag_integrity"
down_revision = "0003_link_revocation"
branch_labels = None
depends_on = None

_REACH = (
    "WITH RECURSIVE reach(n) AS (SELECT NEW.to_memory_id UNION "
    "SELECT e.to_memory_id FROM memory_edges e JOIN reach r ON e.from_memory_id = r.n "
    "WHERE e.status = 'active'{excl}) SELECT 1 FROM reach WHERE n = NEW.from_memory_id"
)

TRIGGERS = {
    "trg_memory_edges_no_cycle_insert": (
        "CREATE TRIGGER trg_memory_edges_no_cycle_insert BEFORE INSERT ON memory_edges "
        "WHEN NEW.status = 'active' AND EXISTS (" + _REACH.format(excl="") + ") "
        "BEGIN SELECT RAISE(ABORT, 'DAG_CYCLE_DETECTED: edge would create a cycle'); END"
    ),
    "trg_memory_edges_no_cycle_update": (
        "CREATE TRIGGER trg_memory_edges_no_cycle_update BEFORE UPDATE ON memory_edges "
        "WHEN NEW.status = 'active' AND EXISTS ("
        + _REACH.format(excl=" AND e.edge_id <> OLD.edge_id")
        + ") BEGIN SELECT RAISE(ABORT, 'DAG_CYCLE_DETECTED: edge would create a cycle'); END"
    ),
    "trg_memory_edges_endpoints_insert": (
        "CREATE TRIGGER trg_memory_edges_endpoints_insert BEFORE INSERT ON memory_edges "
        "WHEN NOT EXISTS (SELECT 1 FROM memories WHERE memory_id = NEW.from_memory_id) "
        "OR NOT EXISTS (SELECT 1 FROM memories WHERE memory_id = NEW.to_memory_id) "
        "BEGIN SELECT RAISE(ABORT, 'EDGE_ENDPOINT_MISSING: memory does not exist'); END"
    ),
    "trg_memory_relations_endpoints_insert": (
        "CREATE TRIGGER trg_memory_relations_endpoints_insert BEFORE INSERT ON memory_relations "
        "WHEN NOT EXISTS (SELECT 1 FROM memories WHERE memory_id = NEW.from_memory_id) "
        "OR NOT EXISTS (SELECT 1 FROM memories WHERE memory_id = NEW.to_memory_id) "
        "BEGIN SELECT RAISE(ABORT, 'EDGE_ENDPOINT_MISSING: memory does not exist'); END"
    ),
}


def _find_cycle_edges(rows: list[tuple[str, str, str]]) -> list[str]:
    """Kahn's algorithm over active edges; returns edge_ids left on cycles (empty = DAG)."""
    indeg: dict[str, int] = defaultdict(int)
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    nodes: set[str] = set()
    for eid, a, b in rows:
        out[a].append((eid, b))
        indeg[b] += 1
        nodes.update((a, b))
    q = deque(n for n in nodes if indeg[n] == 0)
    while q:
        n = q.popleft()
        for _eid, m in out[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    left = {n for n in nodes if indeg[n] > 0}
    return sorted(eid for eid, a, b in rows if a in left and b in left)


def record_fingerprints(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
    ).fetchall()
    fp = {n: hashlib.sha256(" ".join(s.split()).encode()).hexdigest() for n, s in rows}
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    bind.execute(
        sa.text(
            "INSERT INTO ame_meta(key, value, updated_at) VALUES ('trigger_fingerprints', :v, :t) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at"
        ),
        {"v": json.dumps(fp, sort_keys=True), "t": now},
    )


def upgrade() -> None:
    bind = op.get_bind()
    rows = [
        (r[0], r[1], r[2])
        for r in bind.execute(
            sa.text(
                "SELECT edge_id, from_memory_id, to_memory_id FROM memory_edges "
                "WHERE status = 'active'"
            )
        )
    ]
    cyc = _find_cycle_edges(rows)
    if cyc:
        raise RuntimeError(
            "0004 aborted: existing active structural edges contain a cycle; no change made. "
            f"edge_ids={cyc}. Run `ame check-graph --repair-dry-run`."
        )
    op.create_index(
        "ix_memory_edges_active_adj", "memory_edges", ["from_memory_id", "to_memory_id"],
        sqlite_where=sa.text("status = 'active'"),
    )  # fmt: skip
    for sql in TRIGGERS.values():
        op.execute(sql)
    record_fingerprints(bind)


def downgrade() -> None:
    # Structural rollback is safe here (no data is transformed): drop guards and index.
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index("ix_memory_edges_active_adj", table_name="memory_edges")
    record_fingerprints(op.get_bind())

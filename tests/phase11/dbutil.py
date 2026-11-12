"""Raw-SQL helpers for Phase 1.1 tests (simulate old databases / tampering)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ame.infrastructure.migration_runner import MigrationRunner

MEM_COLS = (
    "memory_id, memory_type, layer, scope_type, scope_id, created_by, created_at, updated_at, "
    "importance, confidence, status, current_version, metadata_json"
)


def connect(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db), isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def add_memory(c: sqlite3.Connection, mid: str, status: str = "active", content: str = "c") -> None:
    c.execute(
        f"INSERT INTO memories ({MEM_COLS}) VALUES (?, 'semantic', 'L2', 'shared', 'shared', "  # noqa: S608
        "'user:owner', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 0.5, 0.5, ?, 1, '{}')",
        (mid, status),
    )
    c.execute(
        "INSERT INTO memory_versions (version_id, memory_id, version, content, summary, "
        "content_hash, created_by, created_at, change_reason) VALUES (?, ?, 1, ?, NULL, ?, "
        "'user:owner', '2026-01-01T00:00:00Z', NULL)",
        (f"ver_{mid}", mid, content, f"hash_{mid}"),
    )


def add_edge_0002(c: sqlite3.Connection, eid: str, a: str, b: str, t: str = "PARENT") -> None:
    c.execute(
        "INSERT INTO memory_edges (edge_id, from_memory_id, to_memory_id, edge_type, created_by, "
        "created_at, metadata_json) VALUES (?, ?, ?, ?, 'u', '2026-01-01T00:00:00Z', '{}')",
        (eid, a, b, t),
    )


def seeded_0002(db: Path) -> dict[str, int]:
    """A realistic 0002 database: memories, versions, edges (incl. SUPPORTS), CONTRADICTS,
    legacy audit rows and a conflicted pair. Returns row counts."""
    MigrationRunner(db).upgrade("0002_core_memory_tables")
    c = connect(db)
    for i in range(6):
        add_memory(c, f"m{i}", "active")
    add_memory(c, "cA", "conflicted")
    add_memory(c, "cB", "conflicted")
    add_memory(c, "arch", "archived")
    add_edge_0002(c, "e1", "m0", "m1")
    add_edge_0002(c, "e2", "m1", "m2", "REFERENCE")
    add_edge_0002(c, "e3", "m3", "m4", "SUPPORTS")
    c.execute(
        "INSERT INTO memory_relations (relation_id, relation_type, low_memory_id, high_memory_id, "
        "created_by, created_at, metadata_json) VALUES ('r1', 'CONTRADICTS', 'cA', 'cB', 'u', "
        "'2026-01-01T00:00:00Z', '{}')"
    )
    for n, (mid, ev, frm, to, trig) in enumerate(
        [
            ("m0", "created", None, "active", "T2"),
            ("cA", "created", None, "active", "T2"),
            ("cA", "status_changed", "active", "conflicted", "T5"),
            ("arch", "created", None, "active", "T2"),
            ("arch", "status_changed", "active", "archived", "T9"),
        ]
    ):
        c.execute(
            "INSERT INTO memory_audit_events (audit_id, memory_id, event_type, from_value, "
            'to_value, "trigger", actor_type, actor_id, reason, created_at) VALUES '
            "(?, ?, ?, ?, ?, ?, 'user', 'user:owner', 'legacy', ?)",
            (f"aud{n}", mid, ev, frm, to, trig, f"2026-01-01T00:00:0{n}Z"),
        )
    counts = {
        t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("memories", "memory_versions", "memory_edges", "memory_relations",
                  "memory_audit_events")
    }  # fmt: skip
    c.close()
    return counts

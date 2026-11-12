"""Read-only access for `ame check-graph` (ADR-011). Opens SQLite with mode=ro so the
checker can never modify the database. All SQL for integrity checks lives here."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ame.domain.text_normalize import compact_sql

STRUCTURAL = ("PARENT", "REFERENCE", "SUPERSEDES", "DERIVED_FROM")


def fingerprint(sql: str) -> str:
    return hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()


class IntegrityRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        uri = db_path.resolve().as_uri() + "?mode=ro"
        self._c = sqlite3.connect(uri, uri=True)
        self._c.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self._c.close()

    def _all(self, sql: str, *args: Any) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in self._c.execute(sql, args).fetchall()]

    def tables(self) -> set[str]:
        return {r[0] for r in self._all("SELECT name FROM sqlite_master WHERE type='table'")}

    def columns(self, table: str) -> set[str]:
        return {str(r[1]) for r in self._all(f"PRAGMA table_info({table})")}

    def revision(self) -> str | None:
        if "alembic_version" not in self.tables():
            return None
        rows = self._all("SELECT version_num FROM alembic_version")
        return str(rows[0][0]) if rows else None

    def integrity_check(self) -> list[str]:
        return [str(r[0]) for r in self._all("PRAGMA integrity_check")]

    def foreign_key_check(self) -> list[tuple[Any, ...]]:
        return self._all("PRAGMA foreign_key_check")

    def active_structural_edges(self) -> list[tuple[str, str, str, str]]:
        q = ",".join("?" * len(STRUCTURAL))
        return [
            (str(a), str(b), str(c), str(d))
            for a, b, c, d in self._all(
                "SELECT edge_id, from_memory_id, to_memory_id, edge_type FROM memory_edges "
                f"WHERE status='active' AND edge_type IN ({q}) ORDER BY edge_id",  # noqa: S608
                *STRUCTURAL,
            )
        ]

    def memory_ids(self) -> list[str]:
        return [str(r[0]) for r in self._all("SELECT memory_id FROM memories ORDER BY memory_id")]

    def dangling_edges(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT e.edge_id, e.from_memory_id, e.to_memory_id FROM memory_edges e "
            "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id=e.from_memory_id) "
            "OR NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id=e.to_memory_id)"
        )

    def dangling_relations(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT r.relation_id, r.from_memory_id, r.to_memory_id FROM memory_relations r "
            "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id=r.from_memory_id) "
            "OR NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id=r.to_memory_id)"
        )

    def duplicate_active_edges(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT from_memory_id, to_memory_id, edge_type, count(*) FROM memory_edges "
            "WHERE status='active' GROUP BY 1,2,3 HAVING count(*) > 1"
        )

    def unnormalized_undirected_relations(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT relation_id, relation_type FROM memory_relations "
            "WHERE relation_type IN ('CONTRADICTS','RELATED_TO') AND from_memory_id > to_memory_id"
        )

    def superseded_without_edge(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT m.memory_id FROM memories m WHERE m.status='superseded' AND NOT EXISTS "
            "(SELECT 1 FROM memory_edges e WHERE e.to_memory_id=m.memory_id "
            "AND e.edge_type='SUPERSEDES' AND e.status='active')"
        )

    def revoked_links_missing_metadata(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT 'edge', edge_id FROM memory_edges WHERE status='revoked' AND "
            "(revoked_at IS NULL OR revoked_by IS NULL OR revoked_reason IS NULL) "
            "UNION ALL SELECT 'relation', relation_id FROM memory_relations WHERE "
            "status='revoked' AND (revoked_at IS NULL OR revoked_by IS NULL "
            "OR revoked_reason IS NULL)"
        )

    def conflicted_without_open_conflict(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT m.memory_id FROM memories m WHERE m.status='conflicted' AND NOT EXISTS "
            "(SELECT 1 FROM memory_conflicts c WHERE c.status='open' "
            "AND m.memory_id IN (c.memory_a_id, c.memory_b_id))"
        )

    def open_conflicts_on_revoked_relation(self) -> list[tuple[Any, ...]]:
        return self._all(
            "SELECT c.conflict_id, c.relation_id FROM memory_conflicts c "
            "JOIN memory_relations r ON r.relation_id=c.relation_id "
            "WHERE c.status='open' AND r.status<>'active'"
        )

    def triggers(self) -> dict[str, str]:
        return {
            str(n): str(s)
            for n, s in self._all("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
        }

    def stored_fingerprints(self) -> dict[str, str]:
        if "ame_meta" not in self.tables():
            return {}
        rows = self._all("SELECT value FROM ame_meta WHERE key='trigger_fingerprints'")
        return dict(json.loads(rows[0][0])) if rows else {}

    def lexical_index_mismatches(self) -> list[str]:
        """G012: each memory must have exactly one memory_fts row for its current version."""
        rows = self._all(
            "SELECT m.memory_id, m.current_version, COUNT(f.memory_id), MAX(f.version) "
            "FROM memories m LEFT JOIN memory_fts f ON f.memory_id = m.memory_id "
            "GROUP BY m.memory_id HAVING COUNT(f.memory_id) <> 1 "
            "OR MAX(f.version) <> m.current_version ORDER BY m.memory_id"
        )
        out = [f"{mid}:fts_rows={n},fts_version={v},current={cv}" for mid, cv, n, v in rows]
        orphans = self._all(
            "SELECT DISTINCT memory_id FROM memory_fts WHERE memory_id NOT IN "
            "(SELECT memory_id FROM memories) ORDER BY memory_id"
        )
        return out + [f"{r[0]}:orphan" for r in orphans]

    def compact_index_mismatches(self) -> list[str]:
        """G013: memory_fts_compact must hold exactly one row per memory, for its current
        version, with content/summary equal to the compacted current text (ADR-023)."""
        rows = self._all(
            "SELECT m.memory_id, m.current_version, COUNT(f.memory_id), MAX(f.version), "
            "MAX(f.content = "
            + compact_sql("v.content")
            + " AND f.summary = "
            + compact_sql("COALESCE(v.summary, '')")
            + ") "
            "FROM memories m JOIN memory_versions v ON v.memory_id = m.memory_id "
            "AND v.version = m.current_version "
            "LEFT JOIN memory_fts_compact f ON f.memory_id = m.memory_id "
            "GROUP BY m.memory_id HAVING COUNT(f.memory_id) <> 1 "
            "OR MAX(f.version) <> m.current_version OR MAX(f.content = "
            + compact_sql("v.content")
            + " AND f.summary = "
            + compact_sql("COALESCE(v.summary, '')")
            + ") IS NOT 1 ORDER BY m.memory_id"
        )
        out = [
            f"{mid}:compact_rows={n},compact_version={v},current={cv},text_ok={ok}"
            for mid, cv, n, v, ok in rows
        ]
        orphans = self._all(
            "SELECT DISTINCT memory_id FROM memory_fts_compact WHERE memory_id NOT IN "
            "(SELECT memory_id FROM memories) ORDER BY memory_id"
        )
        return out + [f"{r[0]}:orphan" for r in orphans]

    def bad_space_members(self) -> list[str]:
        """G014 (Phase 3a): space_members must reference an existing space/agent, carry its
        grant metadata, and revoked_at/revoked_by must be both set or both empty."""
        rows = self._all(
            "SELECT sm.space_id || '/' || sm.agent_id FROM space_members sm "
            "LEFT JOIN spaces s ON s.space_id = sm.space_id "
            "LEFT JOIN agents a ON a.agent_id = sm.agent_id "
            "WHERE s.space_id IS NULL OR a.agent_id IS NULL "
            "OR sm.granted_at IS NULL OR sm.granted_by IS NULL "
            "OR (sm.revoked_at IS NULL) <> (sm.revoked_by IS NULL) "
            "ORDER BY 1"
        )
        return [str(r[0]) for r in rows]

    def bad_phase4_rows(self) -> list[str]:
        """G015 (Phase 4): feedback/epoch rows must reference existing rows; a closed epoch
        must have end_at; member removal metadata must be both set or both empty; no member
        may be added or removed after its epoch closed."""
        tables = self.tables()
        out: list[str] = []
        if "memory_feedback" in tables:
            out += [
                f"feedback:{r[0]}"
                for r in self._all(
                    "SELECT f.feedback_id FROM memory_feedback f LEFT JOIN memories m "
                    "ON m.memory_id = f.memory_id WHERE m.memory_id IS NULL ORDER BY 1"
                )
            ]
        if "epochs" in tables:
            out += [
                f"epoch:{r[0]}"
                for r in self._all(
                    "SELECT e.epoch_id FROM epochs e LEFT JOIN spaces s ON s.space_id = e.space_id "
                    "WHERE (e.space_id IS NOT NULL AND s.space_id IS NULL) "
                    "OR (e.status = 'closed' AND e.end_at IS NULL) ORDER BY 1"
                )
            ]
            out += [
                f"epoch_member:{r[0]}"
                for r in self._all(
                    "SELECT em.epoch_id || '/' || em.memory_id FROM epoch_members em "
                    "LEFT JOIN epochs e ON e.epoch_id = em.epoch_id "
                    "LEFT JOIN memories m ON m.memory_id = em.memory_id "
                    "WHERE e.epoch_id IS NULL OR m.memory_id IS NULL "
                    "OR (em.removed_at IS NULL) <> (em.removed_by IS NULL) "
                    "OR (e.status = 'closed' AND (em.added_at > e.end_at "
                    "    OR (em.removed_at IS NOT NULL AND em.removed_at > e.end_at))) "
                    "ORDER BY 1"
                )
            ]
        return out

    def bad_distill_jobs(self) -> list[str]:
        """G016 (Phase 4): every distill output must exist, have a DERIVED_FROM edge from each
        input, and be candidate unless a user-made status change is recorded in the audit."""
        out: list[str] = []
        for job_id, inputs_json, out_id in self._all(
            "SELECT job_id, input_ids_json, output_memory_id FROM distill_jobs ORDER BY job_id"
        ):
            row = self._all("SELECT status FROM memories WHERE memory_id = ?", out_id)
            if not row:
                out.append(f"{job_id}:output_missing")
                continue
            try:
                inputs = [str(x) for x in json.loads(inputs_json)]
            except ValueError:
                out.append(f"{job_id}:bad_input_ids")
                continue
            edges = {
                str(r[0])
                for r in self._all(
                    "SELECT from_memory_id FROM memory_edges WHERE to_memory_id = ? "
                    "AND edge_type = 'DERIVED_FROM'",
                    out_id,
                )
            }
            missing = [i for i in inputs if i not in edges]
            if missing:
                out.append(f"{job_id}:missing_derived_from:{','.join(missing)}")
            if row[0][0] != "candidate":
                n = self._all(
                    "SELECT COUNT(*) FROM audit_events WHERE memory_id = ? AND actor_type = 'user' "
                    "AND event_type IN ('MEMORY_PROMOTED', 'MEMORY_REJECTED', "
                    "'MEMORY_STATUS_CHANGED')",
                    out_id,
                )[0][0]
                if not n:
                    out.append(f"{job_id}:status_{row[0][0]}_without_user_audit")
        return out

    def multiple_active_parents(self) -> list[str]:
        """G017 (ADR-033, Conflux parent tree): memories with >1 active incoming PARENT edge."""
        return [
            f"{mid}:{n}"
            for mid, n in self._all(
                "SELECT to_memory_id, COUNT(*) FROM memory_edges "
                "WHERE status = 'active' AND edge_type = 'PARENT' "
                "GROUP BY to_memory_id HAVING COUNT(*) > 1 ORDER BY to_memory_id"
            )
        ]

    def schema_sql(self) -> list[str]:
        """DDL (tables first, then indexes/triggers) to build an in-memory clone for G008."""
        rows = self._all(
            "SELECT type, sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 "
            "WHEN 'index' THEN 1 ELSE 2 END, name"
        )
        out: list[tuple[str, str]] = [(str(t), str(s)) for t, s in rows]
        # FTS5 shadow tables (<vtab>_data, _idx, ...) are created by their virtual table;
        # replaying them separately would fail, so skip them (Phase 2, ADR-020).
        vtabs = [
            s.split()[3] for t, s in out if t == "table" and s.upper().startswith("CREATE VIRTUAL")
        ]
        names = self._all("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        # Match exact FTS5 shadow suffixes: 'memory_fts_compact' (ADR-023) is itself a virtual
        # table, not a shadow of 'memory_fts'.
        sfx = ("_data", "_idx", "_content", "_docsize", "_config")
        shadow = {str(s) for n, s in names if any(str(n) in {v + x for x in sfx} for v in vtabs)}
        return [s for _t, s in out if s not in shadow]

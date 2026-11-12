"""SQL for search, neighbours, graph and stats (Phase 2). All reads; used through a UoW."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ame.domain.text_normalize import compact, compact_sql

STRUCTURAL = ("PARENT", "REFERENCE", "SUPERSEDES", "DERIVED_FROM")
SEMANTIC_LINKS = ("SUPPORTS", "RELATED_TO")  # CONTRADICTS never boosts a ranking


def _in(name: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
    keys = {f"{name}{i}": v for i, v in enumerate(values)}
    return "(" + ", ".join(f":{k}" for k in keys) + ")", keys


def _like_escape(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SearchRepository:
    def __init__(self, session: Session) -> None:
        self._s = session
        self._compact: bool | None = None

    def _rows(self, sql: str, params: dict[str, Any]) -> list[Any]:
        return list(self._s.execute(text(sql), params).all())

    def allowed_ids(
        self,
        scope_ids: Sequence[str],
        layers: Sequence[str],
        memory_types: Sequence[str],
        statuses: Sequence[str],
    ) -> set[str]:
        """Pre-recall filter (scope/layer/type/status). Applied BEFORE any recall (P2-7)."""
        where, params = [], {}
        for col, vals, key in (
            ("scope_id", scope_ids, "sc"),
            ("layer", layers, "ly"),
            ("memory_type", memory_types, "mt"),
            ("status", statuses, "st"),
        ):
            if vals:
                frag, p = _in(key, vals)
                where.append(f"{col} IN {frag}")
                params.update(p)
        sql = "SELECT memory_id FROM memories"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return {r[0] for r in self._rows(sql, params)}

    def term_hits(self, term: str) -> set[str]:
        """Memory ids whose current content/summary/tags contain the term (case-insensitive).
        Terms of >= 3 characters use the FTS5 trigram index; shorter ones fall back to LIKE.
        ADR-023: hits from the whitespace-compacted index (memory_fts_compact) are merged in,
        so '部署流程' also finds '部署 流程'."""
        hits = self._hits("memory_fts", term, ("content", "summary", "tags"))
        if self._has_compact():
            ct = compact(term)
            if ct:
                hits |= self._hits("memory_fts_compact", ct, ("content", "summary"))
        return hits

    def _has_compact(self) -> bool:
        if self._compact is None:
            n = self._s.execute(
                text("SELECT COUNT(*) FROM sqlite_master WHERE name='memory_fts_compact'")
            ).scalar()
            self._compact = bool(n)
        return self._compact

    def _hits(self, table: str, term: str, cols: tuple[str, ...]) -> set[str]:
        if len(term) >= 3:
            phrase = '"' + term.replace('"', '""') + '"'
            sql = f"SELECT memory_id FROM {table} WHERE {table} MATCH :q"  # noqa: S608
            return {r[0] for r in self._rows(sql, {"q": phrase})}
        cond = " OR ".join(f"{c} LIKE :q ESCAPE '\\'" for c in cols)
        sql = f"SELECT memory_id FROM {table} WHERE {cond}"  # noqa: S608
        return {r[0] for r in self._rows(sql, {"q": _like_escape(term)})}

    def links_among(self, ids: Iterable[str]) -> list[tuple[str, str]]:
        """Active structural edges + SUPPORTS/RELATED_TO relations between the given ids."""
        idl = sorted(set(ids))
        if len(idl) < 2:
            return []
        frag, p = _in("i", idl)
        st, p2 = _in("t", STRUCTURAL)
        sm, p3 = _in("r", SEMANTIC_LINKS)
        sql = (
            f"SELECT from_memory_id, to_memory_id FROM memory_edges WHERE status='active' "
            f"AND edge_type IN {st} AND from_memory_id IN {frag} AND to_memory_id IN {frag} "
            f"UNION SELECT from_memory_id, to_memory_id FROM memory_relations "
            f"WHERE status='active' AND relation_type IN {sm} AND from_memory_id IN {frag} "
            f"AND to_memory_id IN {frag}"
        )
        return [(r[0], r[1]) for r in self._rows(sql, {**p, **p2, **p3})]

    def neighbor_links(
        self, memory_id: str, edge_types: Sequence[str], relation_types: Sequence[str]
    ) -> list[tuple[str, str, str, str, str]]:
        """(kind, link_id, type, from, to) of active links touching memory_id."""
        out: list[tuple[str, str, str, str, str]] = []
        if edge_types:
            et, p = _in("t", edge_types)
            sql = (
                "SELECT edge_id, edge_type, from_memory_id, to_memory_id FROM memory_edges "
                f"WHERE status='active' AND edge_type IN {et} "
                "AND (from_memory_id = :m OR to_memory_id = :m) ORDER BY edge_id"
            )
            out += [("edge", *r) for r in self._rows(sql, {**p, "m": memory_id})]
        if relation_types:
            rt, p = _in("t", relation_types)
            sql = (
                "SELECT relation_id, relation_type, from_memory_id, to_memory_id "
                f"FROM memory_relations WHERE status='active' AND relation_type IN {rt} "
                "AND (from_memory_id = :m OR to_memory_id = :m) ORDER BY relation_id"
            )
            out += [("relation", *r) for r in self._rows(sql, {**p, "m": memory_id})]
        return out

    def node_headers(self, ids: Iterable[str]) -> dict[str, tuple[str, str, str, str, str]]:
        """memory_id -> (memory_type, layer, scope_id, status, summary-or-empty). No content."""
        idl = sorted(set(ids))
        if not idl:
            return {}
        frag, p = _in("i", idl)
        sql = (
            "SELECT m.memory_id, m.memory_type, m.layer, m.scope_id, m.status, "
            "COALESCE(v.summary, '') FROM memories m JOIN memory_versions v "
            "ON v.memory_id = m.memory_id AND v.version = m.current_version "
            f"WHERE m.memory_id IN {frag}"
        )
        return {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in self._rows(sql, p)}

    def space_memory_ids(self, space_id: str, limit: int) -> list[str]:
        sql = (
            "SELECT memory_id FROM memories WHERE scope_id = :s AND status <> 'archived' "
            "ORDER BY memory_id LIMIT :n"
        )
        return [r[0] for r in self._rows(sql, {"s": space_id, "n": limit})]

    def all_structural_links(self, ids: Iterable[str]) -> list[tuple[str, str, str, str, str]]:
        idl = sorted(set(ids))
        if not idl:
            return []
        frag, p = _in("i", idl)
        sql = (
            "SELECT 'edge', edge_id, edge_type, from_memory_id, to_memory_id FROM memory_edges "
            f"WHERE status='active' AND from_memory_id IN {frag} AND to_memory_id IN {frag} "
            "UNION ALL SELECT 'relation', relation_id, relation_type, from_memory_id, "
            "to_memory_id FROM memory_relations WHERE status='active' "
            f"AND from_memory_id IN {frag} AND to_memory_id IN {frag} ORDER BY 2"
        )
        return [(r[0], r[1], r[2], r[3], r[4]) for r in self._rows(sql, p)]

    def status_counts_in(self, scope_ids: Sequence[str]) -> dict[str, int]:
        """Phase 3a: per-status counts restricted to the caller's readable spaces."""
        if not scope_ids:
            return {}
        frag, p = _in("sc", scope_ids)
        sql = f"SELECT status, COUNT(*) FROM memories WHERE scope_id IN {frag} GROUP BY status"
        return {str(r[0]): int(r[1]) for r in self._rows(sql, p)}

    def counts(self, sql: str) -> dict[str, int]:
        return {str(r[0]): int(r[1]) for r in self._rows(sql, {})}

    def rebuild_lexical(self) -> int:
        self._s.execute(text("DELETE FROM memory_fts"))
        self._s.execute(
            text(
                "INSERT INTO memory_fts (memory_id, version, content, summary, tags) "
                "SELECT m.memory_id, v.version, v.content, COALESCE(v.summary, ''), "
                "(SELECT COALESCE(group_concat(tag, ' '), '') FROM (SELECT tag FROM memory_tags "
                "t WHERE t.memory_id = m.memory_id ORDER BY tag)) FROM memories m "
                "JOIN memory_versions v ON v.memory_id = m.memory_id "
                "AND v.version = m.current_version ORDER BY m.memory_id"
            )
        )
        if self._has_compact():
            self._s.execute(text("DELETE FROM memory_fts_compact"))
            self._s.execute(
                text(
                    "INSERT INTO memory_fts_compact (memory_id, version, content, summary) "
                    f"SELECT m.memory_id, v.version, {compact_sql('v.content')}, "
                    + compact_sql("COALESCE(v.summary, '')")
                    + " FROM memories m JOIN memory_versions v ON v.memory_id = m.memory_id "
                    "AND v.version = m.current_version ORDER BY m.memory_id"
                )
            )
        n = self._s.execute(text("SELECT COUNT(*) FROM memory_fts")).scalar()
        return int(n or 0)

"""SQL for the outbox and vector_index_state (ADR-021). Ids/versions/hashes only - no memory
text is ever written by this module. Ordering uses rowid (created_at is only ms-precise)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

TS = "%Y-%m-%dT%H:%M:%fZ"
NEW_EVENT_ID = "'evt_' || lower(hex(randomblob(16)))"


class OutboxRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def _rows(self, sql: str, params: dict[str, Any]) -> list[Any]:
        return list(self._s.execute(text(sql), params).all())

    def claim(self, now: str, lease_until: str, limit: int) -> list[tuple[str, str, str, int]]:
        """Claim due pending events and processing events whose lease expired (a crashed
        worker). Returns (event_id, event_type, aggregate_id, attempts)."""
        rows = self._rows(
            "SELECT event_id, event_type, aggregate_id, attempts FROM outbox_events "
            "WHERE (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= :now)) "
            "OR (status = 'processing' AND next_attempt_at <= :now) ORDER BY rowid LIMIT :n",
            {"now": now, "n": limit},
        )
        for r in rows:
            self._s.execute(
                text(
                    "UPDATE outbox_events SET status = 'processing', next_attempt_at = :lease "
                    "WHERE event_id = :e"
                ),
                {"lease": lease_until, "e": r[0]},
            )
        return [(r[0], r[1], r[2], int(r[3])) for r in rows]

    def mark_done(self, event_id: str, now: str) -> None:
        self._s.execute(
            text(
                "UPDATE outbox_events SET status = 'done', processed_at = :now, "
                "next_attempt_at = NULL, last_error = NULL WHERE event_id = :e"
            ),
            {"now": now, "e": event_id},
        )

    def mark_retry(self, event_id: str, attempts: int, nxt: str | None, error: str) -> None:
        status = "failed" if nxt is None else "pending"
        self._s.execute(
            text(
                "UPDATE outbox_events SET status = :st, attempts = :a, next_attempt_at = :nx, "
                "last_error = :err WHERE event_id = :e"
            ),
            {"st": status, "a": attempts, "nx": nxt, "err": error, "e": event_id},
        )

    def retry_failed(self) -> int:
        r = self._s.execute(
            text(
                "UPDATE outbox_events SET status = 'pending', attempts = 0, "
                "next_attempt_at = NULL WHERE status = 'failed'"
            )
        )
        return int(r.rowcount or 0)  # type: ignore[attr-defined]

    def enqueue_reindex_all(self) -> int:
        r = self._s.execute(
            text(
                "INSERT INTO outbox_events (event_id, event_type, aggregate_id, payload_json, "
                f"status, attempts, created_at) SELECT {NEW_EVENT_ID}, 'memory.reindex', "
                "memory_id, json_object('memory_id', memory_id, 'version', current_version), "
                f"'pending', 0, strftime('{TS}', 'now') FROM memories ORDER BY memory_id"
            )
        )
        return int(r.rowcount or 0)  # type: ignore[attr-defined]

    def memory_for_index(self, memory_id: str) -> tuple[str, int, str] | None:
        """(status, current_version, content) - content stays in process memory only."""
        rows = self._rows(
            "SELECT m.status, m.current_version, v.content FROM memories m "
            "JOIN memory_versions v ON v.memory_id = m.memory_id "
            "AND v.version = m.current_version WHERE m.memory_id = :m",
            {"m": memory_id},
        )
        return (rows[0][0], int(rows[0][1]), rows[0][2]) if rows else None

    def set_state(
        self,
        memory_id: str,
        status: str,
        now: str,
        version: int | None = None,
        model: str | None = None,
        dim: int | None = None,
        content_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        self._s.execute(
            text(
                "INSERT INTO vector_index_state (memory_id, status, indexed_version, "
                "embedding_model, embedding_dimension, content_hash, indexed_at, last_error, "
                "updated_at) VALUES (:m, :st, :v, :mo, :d, :h, :ia, :err, :now) "
                "ON CONFLICT(memory_id) DO UPDATE SET status = excluded.status, "
                "indexed_version = COALESCE(excluded.indexed_version, indexed_version), "
                "embedding_model = COALESCE(excluded.embedding_model, embedding_model), "
                "embedding_dimension = COALESCE(excluded.embedding_dimension, "
                "embedding_dimension), content_hash = COALESCE(excluded.content_hash, "
                "content_hash), indexed_at = COALESCE(excluded.indexed_at, indexed_at), "
                "last_error = excluded.last_error, updated_at = excluded.updated_at"
            ),
            {
                "m": memory_id,
                "st": status,
                "v": version,
                "mo": model,
                "d": dim,
                "h": content_hash,
                "ia": now if status == "indexed" else None,
                "err": error,
                "now": now,
            },
        )

    def state_of(self, memory_id: str) -> str | None:
        rows = self._rows(
            "SELECT status FROM vector_index_state WHERE memory_id = :m", {"m": memory_id}
        )
        return str(rows[0][0]) if rows else None

    def outbox_counts(self) -> dict[str, int]:
        return {
            str(r[0]): int(r[1])
            for r in self._rows("SELECT status, COUNT(*) FROM outbox_events GROUP BY status", {})
        }

    def state_counts(self) -> dict[str, int]:
        return {
            str(r[0]): int(r[1])
            for r in self._rows(
                "SELECT status, COUNT(*) FROM vector_index_state GROUP BY status", {}
            )
        }

    def oldest_pending(self) -> str | None:
        rows = self._rows(
            "SELECT MIN(created_at) FROM outbox_events WHERE status IN ('pending','processing')",
            {},
        )
        return rows[0][0] if rows and rows[0][0] else None

    def vector_status(self, memory_id: str, current_version: int) -> str:
        rows = self._rows(
            "SELECT status, indexed_version FROM vector_index_state WHERE memory_id = :m",
            {"m": memory_id},
        )
        if not rows:
            return "pending"
        status, ver = str(rows[0][0]), rows[0][1]
        if status == "indexed" and ver is not None and int(ver) < current_version:
            return "stale"
        return status

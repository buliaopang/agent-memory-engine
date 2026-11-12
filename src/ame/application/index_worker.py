"""Outbox worker (ADR-021, P2-9): keeps the derived vector index in step with SQLite.

- SQLite stays the source of truth; the vector store only gets ids, versions, model, hash and
  vectors - never memory text.
- Claims use next_attempt_at as a lease, so a crashed worker's events are re-claimed later.
- Failures retry with 2^attempts s backoff and become `failed` after MAX_ATTEMPTS. last_error
  holds the exception TYPE only (no text that could echo memory content).
- Without an embedding provider every memory gets state `disabled` and events are done:
  nothing is pretended.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from ame.application.ports import VectorRecord
from ame.application.search_service import VectorBackend
from ame.infrastructure.unit_of_work import UnitOfWork

log = structlog.get_logger()
MAX_ATTEMPTS = 5
LEASE_S = 60
REMOVE_STATUSES = {"archived", "rejected"}


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class RunStats:
    claimed: int = 0
    done: int = 0
    retried: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class IndexWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        backend: VectorBackend | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sf = session_factory
        self._backend = backend
        self._clock = clock

    @property
    def mode(self) -> str:
        return "hybrid" if self._backend else "lexical_graph"

    def run_once(self, batch: int = 100) -> RunStats:
        now = self._clock()
        stats = RunStats()
        with UnitOfWork(self._sf) as uow:
            events = uow.outbox.claim(ts(now), ts(now + timedelta(seconds=LEASE_S)), batch)
            uow.commit()
        stats.claimed = len(events)
        for event_id, _etype, memory_id, attempts in events:
            try:
                self._sync(memory_id)
            except Exception as exc:  # never crash the loop; record the type only
                err = type(exc).__name__
                stats.errors.append(err)
                n = attempts + 1
                nxt = None if n >= MAX_ATTEMPTS else ts(self._clock() + timedelta(seconds=2**n))
                with UnitOfWork(self._sf) as uow:
                    uow.outbox.mark_retry(event_id, n, nxt, err)
                    uow.outbox.set_state(memory_id, "failed", ts(self._clock()), error=err)
                    uow.commit()
                if nxt is None:
                    stats.failed += 1
                else:
                    stats.retried += 1
                log.warning("index_event_failed", event_id=event_id, error=err, attempts=n)
                continue
            with UnitOfWork(self._sf) as uow:
                uow.outbox.mark_done(event_id, ts(self._clock()))
                uow.commit()
            stats.done += 1
        return stats

    def run_until_idle(self, batch: int = 100, max_rounds: int = 1000) -> RunStats:
        total = RunStats()
        for _ in range(max_rounds):
            s = self.run_once(batch)
            if s.claimed == 0:
                break
            total.claimed += s.claimed
            total.done += s.done
            total.retried += s.retried
            total.failed += s.failed
            total.errors += s.errors
        return total

    def _sync(self, memory_id: str) -> None:
        with UnitOfWork(self._sf, read_only=True) as uow:
            row = uow.outbox.memory_for_index(memory_id)
        now = ts(self._clock())
        if row is None:
            return  # memories are never hard-deleted; nothing to do
        status, version, content = row
        if self._backend is None:
            with UnitOfWork(self._sf) as uow:
                uow.outbox.set_state(memory_id, "disabled", now)
                uow.commit()
            return
        if status in REMOVE_STATUSES:
            self._backend.store.delete(memory_id)
            with UnitOfWork(self._sf) as uow:
                uow.outbox.set_state(memory_id, "removed", now)
                uow.commit()
            return
        p = self._backend.provider
        vec = p.embed([content])[0]
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._backend.store.upsert(
            VectorRecord(memory_id, version, p.model_name, p.dimension, h, now, vec)
        )
        with UnitOfWork(self._sf) as uow:
            uow.outbox.set_state(memory_id, "indexed", now, version, p.model_name, p.dimension, h)
            uow.commit()

    # ---- admin operations (CLI `ame index ...`) ----
    def status(self) -> dict[str, Any]:
        with UnitOfWork(self._sf, read_only=True) as uow:
            return {
                "mode": self.mode,
                "outbox": uow.outbox.outbox_counts(),
                "vector_state": uow.outbox.state_counts(),
                "oldest_pending_at": uow.outbox.oldest_pending(),
                "vector_store": self._backend.store.health() if self._backend else None,
            }

    def retry_failed(self) -> int:
        with UnitOfWork(self._sf) as uow:
            n = uow.outbox.retry_failed()
            uow.commit()
        return n

    def rebuild(self) -> int:
        """Drop the derived vector index and re-enqueue every memory (memory.reindex)."""
        if self._backend is not None:
            self._backend.store.clear()
        with UnitOfWork(self._sf) as uow:
            n = uow.outbox.enqueue_reindex_all()
            uow.commit()
        return n

    def rebuild_lexical(self) -> int:
        with UnitOfWork(self._sf) as uow:
            n = uow.search.rebuild_lexical()
            uow.commit()
        return n


class IndexWorkerThread:
    """Background loop inside the server process (P2-9)."""

    def __init__(self, worker: IndexWorker, interval_s: float) -> None:
        self._worker = worker
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.last_run_at: str | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ame-index-worker", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._worker.run_until_idle(max_rounds=10)
                self.last_error = None
            except Exception as exc:  # e.g. database locked; try again next tick
                self.last_error = type(exc).__name__
                log.warning("index_worker_error", error=self.last_error)
            self.last_run_at = ts(datetime.now(UTC))
            self._stop.wait(self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

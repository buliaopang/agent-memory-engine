from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from ame.application.index_worker import MAX_ATTEMPTS, IndexWorker, ts
from ame.application.memory_service import MemoryService
from ame.application.ports import VectorHit, VectorRecord
from ame.application.schemas import StatusChange
from ame.application.search_service import VectorBackend
from ame.cli import main
from ame.config import Settings
from ame.domain.enums import MemoryStatus
from ame.infrastructure.unit_of_work import make_session_factory
from tests.conftest import AGENT, USER, make


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Provider:
    model_name = "test-model"
    dimension = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(t))] for t in texts]


class Store:
    def __init__(self, fail: int = 0) -> None:
        self.rows: dict[str, VectorRecord] = {}
        self.fail = fail

    def upsert(self, record: VectorRecord) -> None:
        if self.fail:
            self.fail -= 1
            raise OSError("secret memory text must not leak")
        self.rows[record.memory_id] = record

    def delete(self, memory_id: str) -> None:
        self.rows.pop(memory_id, None)

    def search(self, vector: list[float], k: int, allowed: set[str]) -> list[VectorHit]:
        return []

    def clear(self) -> None:
        self.rows.clear()

    def count(self) -> int:
        return len(self.rows)

    def health(self) -> dict[str, object]:
        return {"ok": True}


def outbox(engine: Engine) -> list[tuple[str, str, int, str | None]]:
    with engine.connect() as c:
        return [
            (r[0], r[1], r[2], r[3])
            for r in c.execute(
                text(
                    "SELECT event_type, status, attempts, last_error FROM outbox_events "
                    "ORDER BY rowid"
                )  # fmt: skip
            )
        ]


def worker(engine: Engine, store: Store | None, clock: Clock) -> IndexWorker:
    backend = VectorBackend(Provider(), store) if store is not None else None  # type: ignore[arg-type]
    return IndexWorker(make_session_factory(engine), backend, clock)


def test_without_provider_marks_disabled(svc: MemoryService, engine: Engine) -> None:
    m = svc.create_memory(make(content="hello"), USER)
    w = worker(engine, None, Clock())
    assert w.run_until_idle().done == 1
    st = w.status()
    assert st["mode"] == "lexical_graph" and st["vector_state"] == {"disabled": 1}
    assert st["outbox"] == {"done": 1} and st["oldest_pending_at"] is None
    payload = engine.connect().execute(text("SELECT payload_json FROM outbox_events")).scalar()
    assert json.loads(str(payload)) == {"memory_id": m.memory_id, "version": 1}


def test_index_update_archive(svc: MemoryService, engine: Engine) -> None:
    store, clock = Store(), Clock()
    w = worker(engine, store, clock)
    m = svc.create_memory(make(content="hello"), USER)
    w.run_until_idle()
    assert store.rows[m.memory_id].version == 1 and "hello" not in repr(store.rows)
    svc.archive_memory(m.memory_id, "old", USER)
    w.run_until_idle()
    assert m.memory_id not in store.rows
    assert w.status()["vector_state"] == {"removed": 1}


def test_retry_backoff_then_failed_type_only(svc: MemoryService, engine: Engine) -> None:
    store, clock = Store(fail=99), Clock()
    w = worker(engine, store, clock)
    svc.create_memory(make(content="secret"), USER)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        r = w.run_once()
        assert r.claimed == 1, attempt
        assert w.run_once().claimed == 0  # backoff: not due yet
        clock.now += timedelta(seconds=2**attempt + 1)
    rows = outbox(engine)
    assert rows == [("memory.index", "failed", MAX_ATTEMPTS, "OSError")]
    assert w.status()["vector_state"] == {"failed": 1}
    store.fail = 0
    assert w.retry_failed() == 1
    assert w.run_until_idle().done == 1
    assert w.status()["vector_state"] == {"indexed": 1}


def test_crashed_worker_lease_is_reclaimed(svc: MemoryService, engine: Engine) -> None:
    """Acceptance E: interrupted indexing recovers without losing events."""
    store, clock = Store(), Clock()
    svc.create_memory(make(content="a"), USER)
    w = worker(engine, store, clock)
    with engine.begin() as c:  # simulate a worker that claimed and then died
        c.execute(
            text("UPDATE outbox_events SET status='processing', next_attempt_at=:t"),
            {"t": ts(clock.now + timedelta(seconds=60))},
        )
    assert w.run_once().claimed == 0
    clock.now += timedelta(seconds=61)
    assert w.run_once().done == 1 and len(store.rows) == 1


def test_rebuild_reenqueues_everything(svc: MemoryService, engine: Engine) -> None:
    store, clock = Store(), Clock()
    w = worker(engine, store, clock)
    for i in range(3):
        svc.create_memory(make(content=f"m{i}"), USER)
    w.run_until_idle()
    assert w.rebuild() == 3 and store.count() == 0
    assert w.run_until_idle().done == 3 and store.count() == 3
    assert [e for e in outbox(engine) if e[0] == "memory.reindex"] and all(
        e[1] == "done" for e in outbox(engine)
    )


def test_status_change_event(svc: MemoryService, engine: Engine) -> None:
    m = svc.create_memory(make(content="a"), AGENT)
    svc.change_status(m.memory_id, StatusChange(to_status=MemoryStatus.REJECTED, reason="no"), USER)
    store = Store()
    worker(engine, store, Clock()).run_until_idle()
    assert [e[0] for e in outbox(engine)] == ["memory.index", "memory.status"]
    assert store.count() == 0


def test_cli_index(
    settings: Settings, svc: MemoryService, capsys: pytest.CaptureFixture[str]
) -> None:
    svc.create_memory(make(content="a"), USER)
    db = str(settings.db_path)
    assert main(["index", "status", "--db", db, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["outbox"] == {"pending": 1}
    assert main(["index", "run", "--db", db]) == 0
    assert "processed=1" in capsys.readouterr().out
    assert main(["index", "rebuild", "--db", db]) == 0
    assert main(["index", "rebuild-lexical", "--db", db]) == 0
    assert "1 rows" in capsys.readouterr().out
    assert main(["index", "retry-failed", "--db", db]) == 0
    assert main(["index", "status", "--db", db + ".missing"]) == 3

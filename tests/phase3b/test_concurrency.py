"""Phase 3b: simultaneous writers on one SQLite file (WAL + BEGIN IMMEDIATE, ADR-010/027).

No lost writes, no duplicates, audit sequence exact, version races give exactly one winner.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ame.application.access import agent_actor
from ame.application.memory_service import MemoryService
from ame.application.schemas import Actor, MemoryCreate, VersionCreate
from ame.config import Settings
from ame.errors import VersionConflictError
from ame.infrastructure.unit_of_work import make_session_factory
from tests.conftest import register_test_agent
from tests.phase3a.conftest import B, mem

USER = Actor(actor_type="user", actor_id="user:local")


def _run(n: int, fn: Callable[[int], Any]) -> list[Any]:
    """Start n threads behind a barrier so they really collide; collect results/exceptions."""
    out: list[Any] = [None] * n
    gate = threading.Barrier(n)

    def work(i: int) -> None:
        gate.wait()
        try:
            out[i] = fn(i)
        except Exception as exc:  # collected and asserted by the caller
            out[i] = exc

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    return out


def _q(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(settings.db_path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_database_is_wal(engine: Engine, settings: Settings) -> None:
    assert _q(settings, "PRAGMA journal_mode")[0][0] == "wal"


def test_many_agents_write_at_once_nothing_lost_or_duplicated(
    engine: Engine, settings: Settings
) -> None:
    agents = [f"agent:w{i}" for i in range(4)]
    for a in agents:
        register_test_agent(engine, a)
    svc = MemoryService(make_session_factory(engine))
    per = 25

    def write(i: int) -> list[str]:
        actor = agent_actor(agents[i])
        return [
            svc.create_memory(
                MemoryCreate(**mem(content=f"{agents[i]} 第{j}条", tags=[f"t{j % 3}"])), actor
            ).memory_id
            for j in range(per)
        ]

    res = _run(len(agents), write)
    assert all(isinstance(r, list) for r in res), res
    ids = [m for r in res for m in r]
    assert len(ids) == len(set(ids)) == len(agents) * per
    assert _q(settings, "SELECT count(*) FROM memories")[0][0] == len(agents) * per
    assert _q(settings, "SELECT count(*) FROM memories WHERE status != 'candidate'")[0][0] == 0
    # audit: one MEMORY_CREATED per write, attributed to the right agent, seq gap-free
    created = _q(settings, "SELECT actor_id, memory_id FROM audit_events "
                           "WHERE event_type = 'MEMORY_CREATED'")  # fmt: skip
    assert Counter(a for a, _ in created) == {a: per for a in agents}
    assert sorted(m for _, m in created) == sorted(ids)
    seqs = [s for (s,) in _q(settings, "SELECT seq FROM audit_events ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1))
    # each memory has exactly one version and the index outbox got one event per create
    assert _q(settings, "SELECT count(*) FROM memory_versions")[0][0] == len(ids)


def test_version_race_has_exactly_one_winner(engine: Engine, settings: Settings) -> None:
    svc = MemoryService(make_session_factory(engine))
    mid = svc.create_memory(MemoryCreate(**mem()), USER).memory_id
    n = 8

    def bump(i: int) -> Any:
        cmd = VersionCreate(content=f"v2-{i}", change_reason="race", expected_version=1)
        return svc.create_version(mid, cmd, USER)

    res = _run(n, bump)
    wins = [r for r in res if not isinstance(r, Exception)]
    losses = [r for r in res if isinstance(r, Exception)]
    assert len(wins) == 1 and len(losses) == n - 1
    assert all(isinstance(e, VersionConflictError) for e in losses), losses
    assert [
        v
        for (v,) in _q(
            settings,
            f"SELECT version FROM memory_versions "  # noqa: S608
            f"WHERE memory_id = '{mid}' ORDER BY version",
        )
    ] == [1, 2]
    assert _q(settings, "SELECT count(*) FROM audit_events WHERE event_type = "
                        "'MEMORY_VERSION_CREATED'")[0][0] == 1  # fmt: skip


def test_rest_concurrent_same_idempotency_key_writes_once(app_user, settings) -> None:  # type: ignore[no-untyped-def]
    auth = app_user.headers["Authorization"]
    n = 8

    def post(_i: int) -> tuple[int, str | None, Any]:
        c = TestClient(app_user.app)
        r = c.post(f"{B}/memories", json=mem(content="并发同键"),
                   headers={"Authorization": auth, "Idempotency-Key": "race-1"})  # fmt: skip
        return r.status_code, r.headers.get("Idempotency-Replayed"), r.json()

    res = _run(n, post)
    assert all(not isinstance(r, Exception) for r in res), res
    codes = Counter(code for code, _, _ in res)
    assert set(codes) <= {201, 409} and codes[201] >= 1
    assert all(b["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS" for c, _, b in res if c == 409)
    created = [b["memory_id"] for c, _, b in res if c == 201]
    assert len(set(created)) == 1
    assert _q(settings, "SELECT count(*) FROM memories")[0][0] == 1
    assert _q(settings, "SELECT count(*) FROM audit_events "
                        "WHERE event_type = 'MEMORY_CREATED'")[0][0] == 1  # fmt: skip


def test_rest_concurrent_agents_distinct_keys_all_written(app_user, make_agent, settings) -> None:  # type: ignore[no-untyped-def]
    clients = [make_agent(f"agent:r{i}") for i in range(4)]
    per = 10

    def burst(i: int) -> list[int]:
        c = clients[i]
        return [
            c.post(
                f"{B}/memories", json=mem(content=f"r{i}-{j}"), headers={"Idempotency-Key": f"k{j}"}
            ).status_code  # fmt: skip
            for j in range(per)
        ]

    res = _run(len(clients), burst)
    assert all(r == [201] * per for r in res), res
    assert _q(settings, "SELECT count(*) FROM memories")[0][0] == len(clients) * per
    assert _q(settings, "SELECT count(*) FROM idempotency_keys WHERE state = 'done'")[0][0] == (
        len(clients) * per
    )
    seqs = [s for (s,) in _q(settings, "SELECT seq FROM audit_events ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1))

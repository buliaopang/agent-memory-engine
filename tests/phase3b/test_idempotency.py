"""Phase 3b (ADR-027): Idempotency-Key replay, mismatch, per-caller scope, expiry."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ame.application.idempotency_service import IdempotencyService, request_fingerprint
from ame.cli import main
from ame.config import Settings
from ame.errors import IdempotencyInProgressError, IdempotencyKeyMismatchError
from ame.infrastructure.database import create_sqlite_engine, run_migrations
from ame.infrastructure.unit_of_work import make_session_factory
from tests.phase3a.conftest import B, mem


def _db(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.db_path)


def _n(settings: Settings, sql: str) -> int:
    c = _db(settings)
    try:
        return int(c.execute(sql).fetchone()[0])
    finally:
        c.close()


def test_same_key_same_body_writes_once(app_user, settings) -> None:  # type: ignore[no-untyped-def]
    h = {"Idempotency-Key": "create-1"}
    r1 = app_user.post(f"{B}/memories", json=mem(content="一次"), headers=h)
    r2 = app_user.post(f"{B}/memories", json=mem(content="一次"), headers=h)
    r3 = app_user.post(f"{B}/memories", json=mem(content="一次"), headers=h)
    assert r1.status_code == r2.status_code == r3.status_code == 201
    assert "Idempotency-Replayed" not in r1.headers
    assert r2.headers["Idempotency-Replayed"] == "true" and r2.json() == r1.json() == r3.json()
    assert _n(settings, "SELECT count(*) FROM memories") == 1
    assert _n(settings, "SELECT count(*) FROM audit_events WHERE event_type='MEMORY_CREATED'") == 1


def test_same_key_different_body_is_rejected(app_user, settings) -> None:  # type: ignore[no-untyped-def]
    h = {"Idempotency-Key": "k"}
    assert app_user.post(f"{B}/memories", json=mem(content="原文甲"), headers=h).status_code == 201
    r = app_user.post(f"{B}/memories", json=mem(content="原文乙"), headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"
    assert "原文" not in r.text  # no echo of stored or submitted content
    assert _n(settings, "SELECT count(*) FROM memories") == 1


def test_same_key_other_path_is_a_mismatch(app_user) -> None:  # type: ignore[no-untyped-def]
    h = {"Idempotency-Key": "k2"}
    mid = app_user.post(f"{B}/memories", json=mem(), headers=h).json()["memory_id"]
    r = app_user.post(f"{B}/memories/{mid}/archive", json={"reason": "x"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


def test_keys_are_scoped_per_caller(app_user, make_agent, settings) -> None:  # type: ignore[no-untyped-def]
    a, b = make_agent("agent:a"), make_agent("agent:b")
    h = {"Idempotency-Key": "same"}
    ids = {c.post(f"{B}/memories", json=mem(), headers=h).json()["memory_id"]
           for c in (app_user, a, b)}  # fmt: skip
    assert len(ids) == 3
    assert _n(settings, "SELECT count(*) FROM memories") == 3
    rows = _db(settings).execute("SELECT actor_id FROM idempotency_keys ORDER BY 1").fetchall()
    assert [r[0] for r in rows] == ["agent:a", "agent:b", "user:local"]


def test_client_errors_are_replayed_too(app_user) -> None:  # type: ignore[no-untyped-def]
    h = {"Idempotency-Key": "missing"}
    body = {"content": "v2", "change_reason": "r", "expected_version": 1}
    r1 = app_user.post(f"{B}/memories/mem_nope/versions", json=body, headers=h)
    r2 = app_user.post(f"{B}/memories/mem_nope/versions", json=body, headers=h)
    assert r1.status_code == r2.status_code == 404
    assert r2.headers["Idempotency-Replayed"] == "true"


def test_version_conflict_replays_the_original_answer(app_user, settings) -> None:  # type: ignore[no-untyped-def]
    mid = app_user.post(f"{B}/memories", json=mem()).json()["memory_id"]
    body = {"content": "v2", "change_reason": "r", "expected_version": 1}
    h = {"Idempotency-Key": "v2-once"}
    r1 = app_user.post(f"{B}/memories/{mid}/versions", json=body, headers=h)
    r2 = app_user.post(f"{B}/memories/{mid}/versions", json=body, headers=h)
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    r3 = app_user.post(f"{B}/memories/{mid}/versions", json=body)  # no key: real conflict
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "VERSION_CONFLICT"
    assert _n(settings, f"SELECT count(*) FROM memory_versions WHERE memory_id='{mid}'") == 2  # noqa: S608


@pytest.mark.parametrize("bad", ["", "x" * 256, "中文", "a b"])
def test_invalid_key_rejected(app_user, settings, bad) -> None:  # type: ignore[no-untyped-def]
    r = app_user.post(f"{B}/memories", json=mem(), headers={"Idempotency-Key": bad.encode()})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _n(settings, "SELECT count(*) FROM memories") == 0


def test_unauthenticated_with_key_is_401_and_not_stored(app_user, settings) -> None:  # type: ignore[no-untyped-def]
    h = {"Idempotency-Key": "k", "Authorization": "Bearer nope"}
    r = app_user.post(f"{B}/memories", json=mem(), headers=h)
    assert r.status_code == 401
    assert _n(settings, "SELECT count(*) FROM idempotency_keys") == 0


def test_get_with_key_is_ignored(app_user, settings) -> None:  # type: ignore[no-untyped-def]
    r = app_user.get(f"{B}/stats", headers={"Idempotency-Key": "g"})
    assert r.status_code == 200
    assert _n(settings, "SELECT count(*) FROM idempotency_keys") == 0


def test_key_and_token_not_logged(app_user, capsys) -> None:  # type: ignore[no-untyped-def]
    app_user.post(f"{B}/memories", json=mem(), headers={"Idempotency-Key": "secret-key-xyz"})
    out = capsys.readouterr()
    tok = app_user.headers["Authorization"].split()[1]
    for s in ("secret-key-xyz", tok):
        assert s not in out.out and s not in out.err


def _svc(settings: Settings, ttl: float = 24.0) -> IdempotencyService:
    run_migrations(settings)
    return IdempotencyService(make_session_factory(create_sqlite_engine(settings)), ttl)


def test_service_pending_mismatch_abandon_and_expiry(settings) -> None:  # type: ignore[no-untyped-def]
    s = _svc(settings)
    fp = request_fingerprint("POST", "/x", "", b"{}")
    t0 = datetime(2026, 11, 3, tzinfo=UTC)
    assert s.begin("user:local", "k", fp, t0) is None
    with pytest.raises(IdempotencyInProgressError):
        s.begin("user:local", "k", fp, t0)
    with pytest.raises(IdempotencyKeyMismatchError):
        s.begin("user:local", "k", fp + "0", t0)
    s.abandon("user:local", "k")  # e.g. a 5xx: retry is allowed to run again
    assert s.begin("user:local", "k", fp, t0) is None
    s.finish("user:local", "k", 201, '{"ok":1}')
    rep = s.begin("user:local", "k", fp, t0 + timedelta(hours=23, minutes=59))
    assert rep is not None and rep.status_code == 201 and rep.body == '{"ok":1}'
    # after 24 h the record is gone and the key may be reused (even with another body)
    assert s.begin("user:local", "k", fp + "0", t0 + timedelta(hours=24, seconds=1)) is None


def test_purge_only_touches_idempotency_rows(app_user, settings) -> None:  # type: ignore[no-untyped-def]
    for i in range(3):
        app_user.post(
            f"{B}/memories", json=mem(content=f"p{i}"), headers={"Idempotency-Key": f"p{i}"}
        )
    audit = _n(settings, "SELECT count(*) FROM audit_events")
    s: IdempotencyService = app_user.app.state.idempotency_service
    assert s.purge_expired(datetime.now(UTC)) == 0
    assert s.purge_expired(datetime.now(UTC) + timedelta(hours=25)) == 3
    assert _n(settings, "SELECT count(*) FROM idempotency_keys") == 0
    assert _n(settings, "SELECT count(*) FROM audit_events") == audit
    assert _n(settings, "SELECT count(*) FROM memories") == 3


def test_cli_status_and_purge(app_user, settings, capsys) -> None:  # type: ignore[no-untyped-def]
    app_user.post(f"{B}/memories", json=mem(), headers={"Idempotency-Key": "c1"})
    db = str(settings.db_path)
    assert main(["idempotency", "status", "--db", db]) == 0
    assert "done=1 pending=0" in capsys.readouterr().out
    assert main(["idempotency", "purge", "--db", db]) == 0
    assert "purged 0" in capsys.readouterr().out


def test_cli_missing_db(tmp_path: Path) -> None:
    assert main(["idempotency", "status", "--db", str(tmp_path / "none.db")]) == 3

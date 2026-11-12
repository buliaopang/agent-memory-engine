"""Phase 4 infrastructure: migrations 0012-0015 on a populated DB, token rotation without a
restart (L4), integrity checks G015/G016 on a broken DB, scoring units, CLI and MCP tools."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient

from ame.application.integrity_service import IntegrityService
from ame.config import Settings
from ame.errors import MigrationFailedError
from ame.infrastructure.migration_runner import MigrationRunner
from ame.infrastructure.schema_guards import HEAD
from tests.phase3a.test_migrations_3a import _audit_fp, _cfg, _counts, _populated_0008
from tests.phase4.conftest import B, write

P4_TABLES = {"memory_feedback", "epochs", "epoch_members", "distill_jobs"}


def _rev(db: Path) -> str:
    c = sqlite3.connect(db)
    try:
        return str(c.execute("SELECT version_num FROM alembic_version").fetchone()[0])
    finally:
        c.close()


def _tables(db: Path) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def _indexes(db: Path) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        c.close()


def _populated_0011(db: Path) -> None:
    _populated_0008(db)
    MigrationRunner(db).upgrade("0011_idempotency")


# ---------------- migrations ----------------
def test_upgrade_downgrade_upgrade_populated_0011_to_head(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _populated_0011(db)
    fp0, counts0 = _audit_fp(db), _counts(db)
    rep = MigrationRunner(db).upgrade()
    assert rep.to_revision == HEAD == "0016_single_parent" and rep.backup is not None
    assert P4_TABLES <= _tables(db) and "ix_agents_credential_hash" in _indexes(db)
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert IntegrityService(db).run().exit_code == 0
    command.downgrade(_cfg(db), "0011_idempotency")  # empty Phase 4 tables: allowed
    assert _rev(db) == "0011_idempotency" and not (P4_TABLES & _tables(db))
    assert "ix_agents_credential_hash" not in _indexes(db)
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert IntegrityService(db).run().exit_code == 0
    MigrationRunner(db).upgrade()
    assert _rev(db) == HEAD and P4_TABLES <= _tables(db)
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert IntegrityService(db).run().exit_code == 0


def test_each_step_up_down_up(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _populated_0011(db)
    fp0 = _audit_fp(db)
    for prev, nxt in (("0011_idempotency", "0012_agent_key_index"),
                      ("0012_agent_key_index", "0013_feedback"),
                      ("0013_feedback", "0014_epochs"),
                      ("0014_epochs", "0015_distill_jobs")):  # fmt: skip
        MigrationRunner(db).upgrade(nxt)
        command.downgrade(_cfg(db), prev)
        assert _rev(db) == prev
        MigrationRunner(db).upgrade(nxt)
        assert _rev(db) == nxt and _audit_fp(db) == fp0
    assert IntegrityService(db).run().exit_code == 0


def test_downgrade_refuses_when_phase4_data_exists(
    app_user: TestClient, settings: Settings
) -> None:
    mid = write(app_user)
    app_user.post(f"{B}/memories/{mid}/feedback", json={"feedback": "useful", "reason": "x"})
    app_user.post(f"{B}/epochs", json={"name": "e", "reason": "r"})
    db = settings.db_path
    fp0 = _audit_fp(db)
    with pytest.raises(RuntimeError, match="refused"):
        command.downgrade(_cfg(db), "0013_feedback")  # 0014 holds an epoch
    # each revision runs in its own transaction: the empty 0015 step went down, then 0014
    # refused; every epoch/feedback row and the whole audit history are intact.
    assert _rev(db) == "0014_epochs" and _audit_fp(db) == fp0
    assert "epochs" in _tables(db) and "memory_feedback" in _tables(db)
    MigrationRunner(db).upgrade()
    assert _rev(db) == HEAD and IntegrityService(db).run().exit_code == 0


def test_injected_failure_restores_backup(tmp_path: Path) -> None:
    db = tmp_path / "ame.db"
    _populated_0011(db)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE epochs (x INTEGER)")  # makes 0014's CREATE TABLE fail
    c.commit()
    c.close()
    fp0, counts0 = _audit_fp(db), _counts(db)
    with pytest.raises(MigrationFailedError) as ei:
        MigrationRunner(db).upgrade()
    assert "restored" in ei.value.message
    assert _rev(db) == "0011_idempotency"
    assert _audit_fp(db) == fp0 and _counts(db) == counts0
    assert "memory_feedback" not in _tables(db)


def test_agent_key_index_is_used(app_user: TestClient, settings: Settings) -> None:
    c = sqlite3.connect(settings.db_path)
    try:
        q = "EXPLAIN QUERY PLAN SELECT agent_id FROM agents WHERE credential_hash = 'x'"
        plan = " ".join(str(r) for r in c.execute(q))
    finally:
        c.close()
    assert "ix_agents_credential_hash" in plan


# ---------------- L4: token rotation without restart ----------------
def test_rotate_token_takes_effect_without_restart(settings: Settings) -> None:
    from ame.adapters.rest.app import create_app
    from ame.infrastructure.token_store import default_protector, rotate_token
    from tests.conftest import read_user_token

    with TestClient(create_app(settings)) as c:
        old = read_user_token(settings.data_dir)
        h_old = {"Authorization": f"Bearer {old}"}
        assert c.get(f"{B}/spaces", headers=h_old).status_code == 200
        new = rotate_token(settings.data_dir, default_protector())
        assert new != old
        assert c.get(f"{B}/spaces", headers=h_old).status_code == 401
        assert c.get(f"{B}/spaces", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_token_provider_fails_closed(tmp_path: Path) -> None:
    from ame.infrastructure.token_store import (
        TokenProvider,
        default_protector,
        load_or_create_token,
        token_path,
    )

    prot = default_protector()
    tok = load_or_create_token(tmp_path, prot)
    tp = TokenProvider(tmp_path, prot)
    assert tp.get() == tok
    if prot is None:
        p = token_path(tmp_path)
        p.write_text("short")  # invalid token -> no user match
        assert tp.get() is None
        p.unlink()
        assert tp.get() is None


# ---------------- G015 / G016 on a broken DB ----------------
def _codes(db: Path) -> dict[str, list[str]]:
    rep = IntegrityService(db).run()
    return {f.code: f.items for f in rep.findings}


def test_g015_g016_detect_broken_rows(app_user: TestClient, settings: Settings) -> None:
    ids = [write(app_user, content=f"每天早上喝咖啡然后跑步{i}公里", tags=["t"]) for i in range(3)]
    d = app_user.post(f"{B}/distill", json={"scope_id": "shared", "threshold": 0.3,
                                            "reason": "r"}).json()["created"][0]  # fmt: skip
    db = settings.db_path
    assert IntegrityService(db).run().exit_code == 0
    c = sqlite3.connect(db)  # raw connection, foreign keys off: simulate damage
    c.execute("INSERT INTO memory_feedback VALUES ('fbk_x', 'mem_gone', 'user', 'user:x', "
              "'useful', 'r', '2026-11-03T00:00:00Z')")  # fmt: skip
    c.execute("INSERT INTO epochs VALUES ('epc_x', 'bad', 'space:gone', 't', NULL, 'open', "
              "'user:x', 't', '{}')")  # fmt: skip
    c.execute(
        "INSERT INTO distill_jobs VALUES ('dst_x', 'tag_bigram_v1', '{}', ?, 'k-x', ?, "
        "'created', 'user:x', 't')",
        (f'["{ids[0]}", "mem_nolink"]', d["memory_id"]),
    )
    c.commit()
    c.close()
    codes = _codes(db)
    assert "feedback:fbk_x" in codes["G015"] and "epoch:epc_x" in codes["G015"]
    assert any(i.startswith("dst_x:missing_derived_from:mem_nolink") for i in codes["G016"])
    assert IntegrityService(db).run().exit_code == 2


def test_g016_flags_status_change_without_user_audit(
    app_user: TestClient, settings: Settings
) -> None:
    for i in range(3):
        write(app_user, content=f"每天早上喝咖啡然后跑步{i}公里", tags=["t"])
    d = app_user.post(f"{B}/distill", json={"scope_id": "shared", "threshold": 0.3,
                                            "reason": "r"}).json()["created"][0]  # fmt: skip
    c = sqlite3.connect(settings.db_path)
    c.execute("UPDATE memories SET status='active' WHERE memory_id=?", (d["memory_id"],))
    c.commit()
    c.close()
    items = _codes(settings.db_path)["G016"]
    assert any(i.endswith("status_active_without_user_audit") for i in items)


# ---------------- scoring units ----------------
def test_scoring_units() -> None:
    from datetime import UTC, datetime, timedelta

    from ame.domain.phase4_scoring import (
        SpineWeights,
        char_bigrams,
        clusters,
        decay_score,
        feedback_score,
        jaccard,
        latest_per_actor,
        parse_spine_weights,
    )

    t0 = "2026-01-01T00:00:00Z"
    latest = latest_per_actor([("a", "useful", t0), ("a", "wrong", "2026-02-01T00:00:00Z"),
                               ("b", "useful", t0)])  # fmt: skip
    assert latest == {"a": "wrong", "b": "useful"}
    assert feedback_score(latest) == pytest.approx(-1 / 3)
    assert feedback_score({str(i): "useful" for i in range(10)}) == 1.0
    now = datetime(2026, 11, 3, tzinfo=UTC)
    s, age = decay_score(0.8, (now - timedelta(days=90)).isoformat(), now, 90.0, 0.0)
    assert s == pytest.approx(0.4, abs=1e-6) and age == pytest.approx(90, abs=1e-6)
    assert sum(SpineWeights().as_dict().values()) == pytest.approx(1.0)
    w = parse_spine_weights("confirmed=0.4,sources=0.0,feedback=0.2,importance=0.2,freshness=0.2")
    assert w.as_dict()["confirmed"] == 0.4
    with pytest.raises(ValueError):
        parse_spine_weights("confirmed=0.9")  # sum != 1
    g = {k: char_bigrams(v) for k, v in {"a": "咖啡跑步", "b": "咖啡跑步!", "c": "电影"}.items()}
    assert jaccard(g["a"], g["b"]) > 0.5 and jaccard(g["a"], g["c"]) == 0
    assert clusters(["c", "b", "a"], g, 0.5) == clusters(["a", "b", "c"], g, 0.5) == [["a", "b"]]


# ---------------- CLI ----------------
def test_cli_epoch_review_distill_decay(
    app_user: TestClient, make_agent: Callable[[str], TestClient], settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    from ame.cli import main
    from tests.phase4.conftest import grant

    db = str(settings.db_path)
    mid = write(app_user)
    for i in range(3):
        write(app_user, content=f"每天早上喝咖啡然后跑步{i}公里", tags=["t"])
    a = make_agent("agent:cli")
    grant(app_user, "shared", "agent:cli", "editor")
    cand = write(a, content="candidate")
    assert main(["epoch", "--db", db, "create", "中文 阶段", "--reason", "r"]) == 0
    import re

    m = re.search(r"epoch created: (\S+)", capsys.readouterr().out)
    assert m is not None
    eid = m.group(1)
    assert main(["epoch", "--db", db, "add", eid, mid, "--reason", "r"]) == 0
    assert main(["epoch", "--db", db, "list"]) == 0
    assert "中文 阶段" in capsys.readouterr().out
    assert main(["epoch", "--db", db, "remove", eid, mid, "--reason", "r"]) == 0
    assert main(["epoch", "--db", db, "close", eid, "--reason", "r"]) == 0
    assert main(["epoch", "--db", db, "close", eid, "--reason", "r"]) == 3  # already closed
    capsys.readouterr()
    assert main(["distill", "--db", db, "run", "--scope", "shared", "--threshold", "0.3",
                 "--reason", "r"]) == 0  # fmt: skip
    assert "created=1" in capsys.readouterr().out
    assert main(["review", "--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert cand in out and "distill" in out
    assert main(["review", "--db", db, "reject", cand, "--reason", "no"]) == 0
    assert "rejected" in capsys.readouterr().out
    assert main(["decay", "--db", db, "suggest"]) == 0
    assert "nothing archived" in capsys.readouterr().out
    assert main(["decay", "--db", db, "archive", mid, "--reason", "x"]) == 3  # not suggested
    assert main(["archive", "--db", db, "--from-suggestion", mid, "--reason", "x"]) == 3
    assert main(["check-graph", "--db", db]) == 0

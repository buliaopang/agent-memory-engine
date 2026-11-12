"""Phase 4 REST behaviour: feedback, epochs, spine, decay, distill, review (ADR-029..032)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from fastapi.testclient import TestClient

from ame.config import Settings
from tests.phase4.conftest import B, grant, space, write


def _status(c: TestClient, mid: str) -> str:
    return str(c.get(f"{B}/memories/{mid}").json()["status"])


def _audit_types(settings: Settings) -> list[str]:
    c = sqlite3.connect(settings.db_path)
    try:
        return [r[0] for r in c.execute("SELECT event_type FROM audit_events ORDER BY seq")]
    finally:
        c.close()


# ---------------- feedback (L6/D11) ----------------
def test_feedback_user_and_agent_latest_vote_counts(
    app_user: TestClient, make_agent: Callable[[str], TestClient], settings: Settings
) -> None:
    mid = write(app_user, content="反馈 feedback target")
    a = make_agent("agent:fb")
    grant(app_user, "shared", "agent:fb", "reader")
    for k in ("useful", "useful", "wrong"):  # anti-spam: only the latest vote per actor counts
        r = a.post(f"{B}/memories/{mid}/feedback", json={"feedback": k, "reason": "测试"})
        assert r.status_code == 201, r.text
        assert r.json()["actor_id"] == "agent:fb"
    s = app_user.get(f"{B}/memories/{mid}/feedback").json()
    assert s["voters"] == 1 and s["counts"]["wrong"] == 1 and s["counts"]["useful"] == 0
    assert len(s["items"]) == 3  # history kept, nothing overwritten
    assert abs(s["score"] - (-2 / 3)) < 1e-5  # -2 / max(3, voters)
    assert "FEEDBACK_ADDED" in _audit_types(settings)
    bad = app_user.post(f"{B}/memories/{mid}/feedback", json={"feedback": "love", "reason": "x"})
    assert bad.status_code == 422


def test_feedback_invisible_memory_is_404(
    app_user: TestClient, make_agent: Callable[[str], TestClient]
) -> None:
    space(app_user, "project:secret", "project")
    mid = write(app_user, scope_id="project:secret", scope_type="project")
    a = make_agent("agent:out")
    r = a.post(f"{B}/memories/{mid}/feedback", json={"feedback": "useful", "reason": "x"})
    assert r.status_code == 404
    assert a.get(f"{B}/memories/{mid}/feedback").status_code == 404


def test_feedback_rows_are_append_only(app_user: TestClient, settings: Settings) -> None:
    mid = write(app_user)
    app_user.post(f"{B}/memories/{mid}/feedback", json={"feedback": "useful", "reason": "x"})
    c = sqlite3.connect(settings.db_path)
    try:
        for sql in ("UPDATE memory_feedback SET feedback='wrong'", "DELETE FROM memory_feedback"):
            try:
                c.execute(sql)
                raise AssertionError(sql)
            except sqlite3.IntegrityError:
                pass
    finally:
        c.close()


def test_feedback_post_is_idempotent(app_user: TestClient) -> None:
    mid = write(app_user)
    h = {"Idempotency-Key": "fb-1"}
    body = {"feedback": "outdated", "reason": "x"}
    r1 = app_user.post(f"{B}/memories/{mid}/feedback", json=body, headers=h)
    r2 = app_user.post(f"{B}/memories/{mid}/feedback", json=body, headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r1.json()["feedback_id"] == r2.json()["feedback_id"]
    assert len(app_user.get(f"{B}/memories/{mid}/feedback").json()["items"]) == 1


# ---------------- epochs (D11) ----------------
def test_epoch_lifecycle_chinese_name_and_search_filter(
    app_user: TestClient, settings: Settings
) -> None:
    m1 = write(app_user, content="苹果 apple 第一阶段")
    m2 = write(app_user, content="苹果 apple 第二阶段")
    r = app_user.post(f"{B}/epochs", json={"name": "2026 秋季 · 搬家", "reason": "新阶段"})
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["name"] == "2026 秋季 · 搬家" and e["status"] == "open" and e["member_ids"] == []
    eid = e["epoch_id"]
    ch = {"memory_id": m1, "reason": "属于这个阶段"}
    assert app_user.post(f"{B}/epochs/{eid}/members", json=ch).json()["member_ids"] == [m1]
    # idempotent add: no second audit row
    n = _audit_types(settings).count("EPOCH_MEMBER_ADDED")
    app_user.post(f"{B}/epochs/{eid}/members", json=ch)
    assert _audit_types(settings).count("EPOCH_MEMBER_ADDED") == n
    s = app_user.post(f"{B}/memories/search", json={"query": "苹果", "epoch_id": eid})
    assert s.status_code == 200, s.text
    ids = [h["memory"]["memory_id"] if "memory" in h else h["memory_id"]
           for h in s.json()["results"]]  # fmt: skip
    assert m1 in ids and m2 not in ids
    missing = app_user.post(f"{B}/memories/search", json={"query": "x", "epoch_id": "epc_none"})
    assert missing.status_code == 404
    # soft remove, then re-add keeps one row
    rm = app_user.post(f"{B}/epochs/{eid}/members/remove", json=ch)
    assert rm.json()["member_ids"] == []
    assert app_user.post(f"{B}/epochs/{eid}/members", json=ch).json()["member_ids"] == [m1]
    cl = app_user.post(f"{B}/epochs/{eid}/close", json={"reason": "结束"})
    assert cl.status_code == 200 and cl.json()["status"] == "closed" and cl.json()["end_at"]
    again = app_user.post(f"{B}/epochs/{eid}/close", json={"reason": "again"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "EPOCH_CLOSED"
    late = app_user.post(f"{B}/epochs/{eid}/members", json={"memory_id": m2, "reason": "x"})
    assert late.status_code == 409
    types = _audit_types(settings)
    for t in ("EPOCH_CREATED", "EPOCH_MEMBER_ADDED", "EPOCH_MEMBER_REMOVED", "EPOCH_CLOSED"):
        assert t in types


def test_epoch_closed_is_enforced_by_database(app_user: TestClient, settings: Settings) -> None:
    mid = write(app_user)
    eid = app_user.post(f"{B}/epochs", json={"name": "e", "reason": "r"}).json()["epoch_id"]
    app_user.post(f"{B}/epochs/{eid}/close", json={"reason": "r"})
    c = sqlite3.connect(settings.db_path)
    try:
        for sql, args in (
            ("INSERT INTO epoch_members VALUES (?, ?, 't', 'user:x', NULL, NULL)", (eid, mid)),
            ("UPDATE epochs SET status='open', end_at=NULL WHERE epoch_id=?", (eid,)),
            ("DELETE FROM epochs WHERE epoch_id=?", (eid,)),
        ):
            try:
                c.execute(sql, args)
                raise AssertionError(sql)
            except sqlite3.IntegrityError:
                pass
    finally:
        c.close()


def test_agents_cannot_change_epochs_but_can_read(
    app_user: TestClient, make_agent: Callable[[str], TestClient]
) -> None:
    mid = write(app_user)
    eid = app_user.post(f"{B}/epochs", json={"name": "e", "reason": "r"}).json()["epoch_id"]
    a = make_agent("agent:ep")
    grant(app_user, "shared", "agent:ep", "editor")
    assert a.post(f"{B}/epochs", json={"name": "x", "reason": "r"}).status_code == 403
    ch = {"memory_id": mid, "reason": "r"}
    assert a.post(f"{B}/epochs/{eid}/members", json=ch).status_code == 403
    assert a.post(f"{B}/epochs/{eid}/close", json={"reason": "r"}).status_code == 403
    assert [e["epoch_id"] for e in a.get(f"{B}/epochs").json()] == [eid]


def test_epoch_space_mismatch_and_hidden_space(
    app_user: TestClient, make_agent: Callable[[str], TestClient]
) -> None:
    space(app_user, "project:p", "project")
    shared = write(app_user)
    eid = app_user.post(f"{B}/epochs", json={"name": "p", "space_id": "project:p",
                                             "reason": "r"}).json()["epoch_id"]  # fmt: skip
    r = app_user.post(f"{B}/epochs/{eid}/members", json={"memory_id": shared, "reason": "r"})
    assert r.status_code == 422
    a = make_agent("agent:nop")
    assert a.get(f"{B}/epochs/{eid}").status_code == 404
    assert a.get(f"{B}/epochs").json() == []


# ---------------- spine (D6/ADR-031) ----------------
def test_spine_ranks_active_with_explanations(
    app_user: TestClient, make_agent: Callable[[str], TestClient]
) -> None:
    hi = write(app_user, content="重要 core", importance=0.95)
    lo = write(app_user, content="琐事 trivia", importance=0.05)
    a = make_agent("agent:sp")
    grant(app_user, "shared", "agent:sp", "editor")
    cand = write(a, content="agent candidate")
    app_user.post(f"{B}/memories/{hi}/feedback", json={"feedback": "useful", "reason": "x"})
    r = app_user.get(f"{B}/spine")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    ids = [i["memory_id"] for i in items]
    assert ids.index(hi) < ids.index(lo) and cand not in ids  # candidates never on the spine
    top = items[0]
    assert top["memory_id"] == hi and len(top["explanation"]) == 5
    assert set(top["components"]) == set(top["weights"]) == {
        "confirmed", "sources", "feedback", "importance", "freshness"}  # fmt: skip
    assert abs(sum(top["weights"].values()) - 1.0) < 1e-9
    assert a.get(f"{B}/spine").status_code == 200  # agents may read


def test_spine_custom_weights(tmp_path_factory: object, settings: Settings) -> None:
    from ame.adapters.rest.app import create_app
    from tests.conftest import read_user_token

    s2 = settings.model_copy(
        update={"spine_weights": "confirmed=0,sources=0,feedback=0,importance=1,freshness=0"}
    )
    with TestClient(create_app(s2)) as c:
        c.headers["Authorization"] = f"Bearer {read_user_token(s2.data_dir)}"
        write(c, importance=0.3)
        it = c.get(f"{B}/spine").json()["items"][0]
        assert it["weights"]["importance"] == 1.0 and abs(it["score"] - 0.3) < 1e-6


def test_bad_spine_weights_fail_at_startup(settings: Settings) -> None:
    import pytest

    from ame.adapters.rest.app import create_app

    for bad in ("confirmed=1,sources=1", "nope=1", "confirmed=x"):
        s2 = settings.model_copy(update={"spine_weights": bad})
        with pytest.raises(Exception):  # noqa: B017
            with TestClient(create_app(s2)):
                pass


# ---------------- decay (D5/ADR-029) ----------------
def _age(settings: Settings, mid: str, days: int) -> None:
    from datetime import UTC, datetime, timedelta

    ts = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    c = sqlite3.connect(settings.db_path)
    c.execute("UPDATE memories SET updated_at=? WHERE memory_id=?", (ts, mid))
    c.commit()
    c.close()


def test_decay_suggest_is_read_only_and_protects(app_user: TestClient, settings: Settings) -> None:
    old = write(app_user, content="旧 old", importance=0.3)
    keep = write(app_user, content="重要但旧", importance=0.9)
    liked = write(app_user, content="被确认有用", importance=0.3)
    fresh = write(app_user, content="新", importance=0.3)
    for m in (old, keep, liked):
        _age(settings, m, 400)
    app_user.post(f"{B}/memories/{liked}/feedback", json={"feedback": "useful", "reason": "x"})
    _age(settings, liked, 400)
    n_audit = len(_audit_types(settings))
    r = app_user.get(f"{B}/decay/suggestions")
    assert r.status_code == 200, r.text
    ids = [s["memory_id"] for s in r.json()["suggestions"]]
    assert ids == [old] and keep not in ids and liked not in ids and fresh not in ids
    assert len(_audit_types(settings)) == n_audit  # suggest writes nothing
    assert _status(app_user, old) == "active"
    assert "suggestion only" in r.json()["suggestions"][0]["explanation"][1]


def test_decay_archive_only_from_suggestion_and_user_only(
    app_user: TestClient, make_agent: Callable[[str], TestClient], settings: Settings
) -> None:
    old = write(app_user, importance=0.2)
    fresh = write(app_user, importance=0.2)
    _age(settings, old, 500)
    a = make_agent("agent:dc")
    grant(app_user, "shared", "agent:dc", "owner")
    assert a.post(f"{B}/decay/archive", json={"memory_id": old, "reason": "x"}).status_code == 403
    nope = app_user.post(f"{B}/decay/archive", json={"memory_id": fresh, "reason": "x"})
    assert nope.status_code == 422
    ok = app_user.post(f"{B}/decay/archive", json={"memory_id": old, "reason": "不用了"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "archived"
    c = sqlite3.connect(settings.db_path)
    reason = c.execute("SELECT reason FROM audit_events WHERE memory_id=? ORDER BY seq DESC",
                       (old,)).fetchone()[0]  # fmt: skip
    c.close()
    assert reason.startswith("decay suggestion score=") and "不用了" in reason


# ---------------- distill + review (D3/D4/D12) ----------------
def _similar(c: TestClient) -> list[str]:
    return [
        write(c, content="每天早上喝咖啡然后跑步五公里", tags=["习惯"]),
        write(c, content="每天早上喝咖啡然后跑步三公里", tags=["习惯"]),
        write(c, content="每天早上喝咖啡然后去跑步", tags=["习惯"]),
        write(c, content="完全不同的内容：周末看电影", tags=["习惯"]),
    ]


def test_distill_creates_candidate_draft_and_keeps_originals(
    app_user: TestClient, settings: Settings
) -> None:
    ids = _similar(app_user)
    before = {i: app_user.get(f"{B}/memories/{i}").json() for i in ids}
    r = app_user.post(f"{B}/distill", json={"scope_id": "shared", "tag": "习惯",
                                            "threshold": 0.4, "reason": "整理"})  # fmt: skip
    assert r.status_code == 201, r.text
    res = r.json()
    assert len(res["created"]) == 1
    d = res["created"][0]
    assert set(d["source_memory_ids"]) == set(ids[:3])
    draft = app_user.get(f"{B}/memories/{d['memory_id']}").json()
    assert draft["status"] == "candidate" and "distill-draft" in draft["tags"]
    assert {i: app_user.get(f"{B}/memories/{i}").json() for i in ids} == before
    edges = app_user.get(f"{B}/memories/{d['memory_id']}/edges").json()
    flat = edges if isinstance(edges, list) else edges.get("incoming", []) + edges.get(
        "outgoing", [])  # fmt: skip
    derived = {e["from_memory_id"] for e in flat if e["edge_type"] == "DERIVED_FROM"}
    assert derived == set(ids[:3])
    # idempotent: same inputs -> no second draft
    r2 = app_user.post(f"{B}/distill", json={"scope_id": "shared", "tag": "习惯",
                                             "threshold": 0.4, "reason": "again"})  # fmt: skip
    assert r2.json()["created"] == [] and r2.json()["skipped_existing"] == 1
    assert "DISTILL_CREATED" in _audit_types(settings)
    from ame.application.integrity_service import IntegrityService

    assert IntegrityService(settings.db_path).run().exit_code == 0


def test_distill_and_review_are_user_only(
    app_user: TestClient, make_agent: Callable[[str], TestClient]
) -> None:
    a = make_agent("agent:ds")
    grant(app_user, "shared", "agent:ds", "owner")
    cand = write(a, content="agent note")
    assert a.post(f"{B}/distill", json={"scope_id": "shared", "reason": "x"}).status_code == 403
    assert a.post(f"{B}/memories/{cand}/promote", json={"reason": "x"}).status_code == 403
    assert a.post(f"{B}/memories/{cand}/reject", json={"reason": "x"}).status_code == 403
    assert _status(app_user, cand) == "candidate"


def test_review_promote_and_reject_audit_sources(
    app_user: TestClient, make_agent: Callable[[str], TestClient], settings: Settings
) -> None:
    ids = _similar(app_user)
    d = app_user.post(f"{B}/distill", json={"scope_id": "shared", "threshold": 0.4,
                                            "reason": "r"}).json()["created"][0]  # fmt: skip
    a = make_agent("agent:rv")
    grant(app_user, "shared", "agent:rv", "editor")
    cand = write(a, content="agent candidate to reject")
    q = app_user.get(f"{B}/review").json()
    by = {i["memory_id"]: i for i in q}
    assert by[d["memory_id"]]["is_distill_draft"] is True
    assert set(by[d["memory_id"]]["source_memory_ids"]) == set(ids[:3])
    assert by[cand]["is_distill_draft"] is False
    p = app_user.post(f"{B}/memories/{d['memory_id']}/promote", json={"reason": "确认"})
    assert p.status_code == 200, p.text
    assert p.json()["status"] == "active"
    j = app_user.post(f"{B}/memories/{cand}/reject", json={"reason": "不对"})
    assert j.status_code == 200 and j.json()["status"] == "rejected"
    twice = app_user.post(f"{B}/memories/{cand}/promote", json={"reason": "x"})
    assert twice.status_code in (409, 422)
    c = sqlite3.connect(settings.db_path)
    rows = c.execute("SELECT event_type, after_json FROM audit_events WHERE event_type IN "
                     "('MEMORY_PROMOTED','MEMORY_REJECTED') ORDER BY seq").fetchall()  # fmt: skip
    c.close()
    assert [r[0] for r in rows] == ["MEMORY_PROMOTED", "MEMORY_REJECTED"]
    assert all(i in rows[0][1] for i in ids[:3])
    from ame.application.integrity_service import IntegrityService

    assert IntegrityService(settings.db_path).run().exit_code == 0


# ---------------- L3 audit redaction ----------------
def test_agent_audit_hides_invisible_memory_ids(
    app_user: TestClient, make_agent: Callable[[str], TestClient]
) -> None:
    space(app_user, "project:hid", "project")
    hidden = write(app_user, scope_id="project:hid", scope_type="project", content="秘密")
    vis = write(app_user, content="公开")
    r = app_user.post(f"{B}/edges", json={"from_memory_id": hidden, "to_memory_id": vis,
                                          "edge_type": "REFERENCE"})  # fmt: skip
    assert r.status_code == 201, r.text
    a = make_agent("agent:aud")
    grant(app_user, "shared", "agent:aud", "reader")
    resp = a.get(f"{B}/memories/{vis}/audit")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert hidden not in body
    assert any(e.get("related_hidden") for e in resp.json())
    assert hidden in app_user.get(f"{B}/memories/{vis}/audit").text  # the user sees all


# ---------------- invariants and visibility ----------------
def _fingerprint(settings: Settings, ids: list[str]) -> str:
    import hashlib

    c = sqlite3.connect(settings.db_path)
    h = hashlib.sha256()
    q = ",".join("?" * len(ids))
    for sql in (f"SELECT * FROM memories WHERE memory_id IN ({q}) ORDER BY memory_id",
                f"SELECT * FROM memory_versions WHERE memory_id IN ({q}) "
                "ORDER BY memory_id, version"):  # fmt: skip
        for row in c.execute(sql, ids):
            h.update(repr(row).encode())
    c.close()
    return h.hexdigest()


def test_distill_and_decay_leave_originals_byte_identical(
    app_user: TestClient, settings: Settings
) -> None:
    ids = _similar(app_user)
    for m in ids:
        _age(settings, m, 300)
    fp = _fingerprint(settings, ids)
    app_user.get(f"{B}/decay/suggestions")
    app_user.get(f"{B}/spine")
    r = app_user.post(f"{B}/distill", json={"scope_id": "shared", "threshold": 0.4,
                                            "reason": "r"})  # fmt: skip
    assert len(r.json()["created"]) == 1
    assert _fingerprint(settings, ids) == fp


def test_spine_decay_review_only_show_visible_memories(
    app_user: TestClient, make_agent: Callable[[str], TestClient], settings: Settings
) -> None:
    space(app_user, "project:priv", "project")
    hidden = write(app_user, scope_id="project:priv", scope_type="project", importance=0.1)
    _age(settings, hidden, 500)
    a = make_agent("agent:vis")
    grant(app_user, "shared", "agent:vis", "reader")
    for path in ("/spine", "/decay/suggestions", "/review"):
        assert hidden not in a.get(f"{B}{path}").text, path
    assert hidden in app_user.get(f"{B}/decay/suggestions").text
    assert a.get(f"{B}/spine", params={"scope_id": "project:priv"}).json()["items"] == []

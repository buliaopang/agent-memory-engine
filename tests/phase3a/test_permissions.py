"""Phase 3a permission matrix (ADR-024/025, PHASE_3_REVIEW decisions 3-7)."""

from __future__ import annotations

import pytest

from tests.phase3a.conftest import B, grant, mem, space


@pytest.fixture()
def world(app_user, make_agent):  # type: ignore[no-untyped-def]
    a, b = make_agent("agent:a"), make_agent("agent:b")
    space(app_user, "project:p", "project")
    space(app_user, "private:a", "private")
    grant(app_user, "private:a", "agent:a", "owner")
    grant(app_user, "project:p", "agent:a", "editor")
    grant(app_user, "project:p", "agent:b", "reader")
    return app_user, a, b


def test_agent_write_is_candidate_and_cannot_approve(world) -> None:  # type: ignore[no-untyped-def]
    user, a, _ = world
    r = a.post(f"{B}/memories", json=mem())
    assert r.status_code == 201
    m = r.json()
    assert m["status"] == "candidate" and m["created_by"] == "agent:a"  # T1 via REST
    s = a.post(f"{B}/memories/{m['memory_id']}/status",
               json={"to_status": "active", "reason": "self-approve"})  # fmt: skip
    assert s.status_code == 403
    ok = user.post(f"{B}/memories/{m['memory_id']}/status",
                   json={"to_status": "active", "reason": "reviewed"})  # fmt: skip
    assert ok.status_code == 200 and ok.json()["status"] == "active"


def test_shared_is_read_write_for_all_enabled_agents(world) -> None:  # type: ignore[no-untyped-def]
    _, a, b = world
    mid = a.post(f"{B}/memories", json=mem(content="共享 部署流程")).json()["memory_id"]
    assert b.get(f"{B}/memories/{mid}").status_code == 200
    assert b.post(f"{B}/memories", json=mem()).status_code == 201
    hits = b.post(f"{B}/memories/search", json={"query": "部署流程"}).json()["results"]
    assert mid in {h["memory"]["memory_id"] for h in hits}


def test_private_invisible_is_404_everywhere(world) -> None:  # type: ignore[no-untyped-def]
    user, a, b = world
    mid = a.post(f"{B}/memories", json=mem("private:a", "private", content="私密 口令xyz")
                 ).json()["memory_id"]  # fmt: skip
    for path in ("", "/versions", "/edges", "/relations", "/audit", "/conflicts", "/neighbors"):
        r = b.get(f"{B}/memories/{mid}{path}")
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "NOT_FOUND"
    missing = b.get(f"{B}/memories/mem_does_not_exist")
    assert missing.status_code == 404  # same answer as "exists but invisible"
    assert b.get(f"{B}/graph", params={"space_id": "private:a"}).status_code == 404
    assert b.get(f"{B}/graph", params={"center_id": mid}).status_code == 404
    assert b.get(f"{B}/spaces/private:a").status_code == 404
    res = b.post(f"{B}/memories/search", json={"query": "口令xyz"}).json()
    assert res["results"] == [] and res["total_candidates"] == 0
    res = b.post(f"{B}/memories/search",
                 json={"query": "口令xyz", "scope_ids": ["private:a"]}).json()  # fmt: skip
    assert res["results"] == []
    assert "private:a" not in {s["space_id"] for s in b.get(f"{B}/spaces").json()}
    # stats: B does not count A's private memory, the user does
    assert b.get(f"{B}/stats").json()["memories"].get("total", 0) == 0
    assert user.get(f"{B}/stats").json()["memories"]["total"] == 1
    assert a.get(f"{B}/memories/{mid}").status_code == 200
    assert user.get(f"{B}/memories/{mid}").status_code == 200
    # writing into an invisible space looks exactly like a missing space
    w = b.post(f"{B}/memories", json=mem("private:a", "private"))
    nx = b.post(f"{B}/memories", json=mem("private:nope", "private"))
    assert w.status_code == nx.status_code == 422
    assert w.json()["error"]["code"] == nx.json()["error"]["code"] == "SPACE_NOT_FOUND"


def test_reader_can_read_but_not_write(world) -> None:  # type: ignore[no-untyped-def]
    _, a, b = world
    mid = a.post(f"{B}/memories", json=mem("project:p", "project")).json()["memory_id"]
    assert b.get(f"{B}/memories/{mid}").status_code == 200
    r = b.post(f"{B}/memories", json=mem("project:p", "project"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"


def test_project_requires_explicit_grant(app_user, make_agent) -> None:  # type: ignore[no-untyped-def]
    c = make_agent("agent:c")
    space(app_user, "project:q", "project")
    mid = app_user.post(f"{B}/memories", json=mem("project:q", "project")).json()["memory_id"]
    assert c.get(f"{B}/memories/{mid}").status_code == 404
    grant(app_user, "project:q", "agent:c", "reader")
    assert c.get(f"{B}/memories/{mid}").status_code == 200
    r = app_user.post(f"{B}/spaces/project:q/members/agent:c/revoke", json={"reason": "done"})
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert c.get(f"{B}/memories/{mid}").status_code == 404


def test_agent_edits_only_own_candidates(world) -> None:  # type: ignore[no-untyped-def]
    user, a, b = world
    mine = a.post(f"{B}/memories", json=mem()).json()["memory_id"]
    ok = a.post(f"{B}/memories/{mine}/versions",
                json={"content": "v2", "expected_version": 1, "change_reason": "fix"})  # fmt: skip
    assert ok.status_code == 201
    assert (
        a.patch(f"{B}/memories/{mine}", json={"importance": 0.9, "reason": "r"}).status_code == 200
    )
    body = {"content": "x", "expected_version": 2, "change_reason": "hijack"}
    other = b.post(f"{B}/memories/{mine}/versions", json=body)
    assert other.status_code == 403
    users = user.post(f"{B}/memories", json=mem()).json()["memory_id"]
    r = a.patch(f"{B}/memories/{users}", json={"importance": 0.1, "reason": "r"})
    assert r.status_code == 403
    user.post(f"{B}/memories/{mine}/status", json={"to_status": "active", "reason": "ok"})
    r = a.post(f"{B}/memories/{mine}/versions",
               json={"content": "v3", "expected_version": 2, "change_reason": "late"})  # fmt: skip
    assert r.status_code == 403  # no longer pending review


def test_only_user_creates_spaces_and_agents(world) -> None:  # type: ignore[no-untyped-def]
    user, a, _ = world
    r = a.post(f"{B}/spaces", json={"space_id": "x", "name": "x", "scope_type": "shared"})
    assert r.status_code == 403
    r = a.post(f"{B}/agents", json={"agent_id": "agent:z", "display_name": "z", "agent_type": "t"})
    assert r.status_code == 403
    assert a.post(f"{B}/agents/agent:b/credentials", json={"reason": "x"}).status_code == 403
    assert a.post(f"{B}/spaces/shared/members",
                  json={"agent_id": "agent:a", "role": "owner", "reason": "x"}
                  ).status_code == 403  # fmt: skip
    assert [x["agent_id"] for x in a.get(f"{B}/agents").json()] == ["agent:a"]
    assert len(user.get(f"{B}/agents").json()) == 2


def test_links_need_visibility_and_write(world) -> None:  # type: ignore[no-untyped-def]
    _, a, b = world
    priv = a.post(f"{B}/memories", json=mem("private:a", "private")).json()["memory_id"]
    pub = b.post(f"{B}/memories", json=mem()).json()["memory_id"]
    r = b.post(f"{B}/relations", json={"from_memory_id": pub, "to_memory_id": priv,
                                       "relation_type": "RELATED_TO"})  # fmt: skip
    assert r.status_code == 404
    r = b.post(f"{B}/memories", json=mem(parent_ids=[priv]))
    assert r.status_code == 404
    # A sees both; A's link from its private memory to shared is fine
    r = a.post(f"{B}/relations", json={"from_memory_id": priv, "to_memory_id": pub,
                                       "relation_type": "RELATED_TO"})  # fmt: skip
    assert r.status_code == 201
    # B's view of pub's relations hides the link into A's private space
    assert b.get(f"{B}/memories/{pub}/relations").json() == []


def test_disabled_agent_and_rotated_key_get_401(app_user, make_agent) -> None:  # type: ignore[no-untyped-def]
    c = make_agent("agent:d")
    assert c.get(f"{B}/stats").status_code == 200  # authenticated
    old = c.headers["Authorization"]
    k = app_user.post(f"{B}/agents/agent:d/credentials", json={"reason": "rotate"}).json()
    assert c.get(f"{B}/stats").status_code == 401  # old key dead immediately
    c.headers["Authorization"] = f"Bearer {k['api_key']}"
    assert c.get(f"{B}/stats").status_code == 200
    assert app_user.post(f"{B}/agents/agent:d/disable", json={"reason": "x"}).status_code == 200
    assert c.get(f"{B}/stats").status_code == 401
    assert app_user.post(f"{B}/agents/agent:d/enable", json={"reason": "x"}).status_code == 200
    assert c.get(f"{B}/stats").status_code == 200
    assert old != c.headers["Authorization"]
    for bad in ("Bearer ame_agt_" + "x" * 43, "Bearer nope", "Basic abc"):
        assert c.get(f"{B}/stats", headers={"Authorization": bad}).status_code == 401


def test_registry_writes_audit(app_user, make_agent, settings) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    make_agent("agent:e")
    space(app_user, "project:e", "project")
    grant(app_user, "project:e", "agent:e", "reader")
    app_user.post(f"{B}/spaces/project:e/members/agent:e/revoke", json={"reason": "r"})
    app_user.post(f"{B}/agents/agent:e/disable", json={"reason": "r"})
    app_user.post(f"{B}/agents/agent:e/enable", json={"reason": "r"})
    c = sqlite3.connect(settings.db_path)
    got = [r[0] for r in c.execute("SELECT event_type FROM audit_events ORDER BY seq")]
    blob = " ".join(str(r) for r in c.execute("SELECT * FROM audit_events"))
    hashes = [r[0] for r in c.execute("SELECT credential_hash FROM agents")]
    c.close()
    assert got == ["AGENT_REGISTERED", "AGENT_CREDENTIAL_ISSUED", "SPACE_CREATED",
                   "SPACE_MEMBER_GRANTED", "SPACE_MEMBER_REVOKED", "AGENT_DISABLED",
                   "AGENT_ENABLED"]  # fmt: skip
    assert "ame_agt_" not in blob  # key never in audit
    assert all(h.startswith("sha256$") and "ame_agt_" not in h for h in hashes)


def test_actor_cannot_be_spoofed(world) -> None:  # type: ignore[no-untyped-def]
    _, a, _ = world
    r = a.post(f"{B}/memories", json=mem(created_by="user:local"))
    assert r.status_code == 422  # unknown field rejected (strict schema)
    r = a.post(f"{B}/memories", json=mem(), headers={"X-Actor": "user:local"})
    assert r.json()["created_by"] == "agent:a" and r.json()["status"] == "candidate"


def test_key_and_token_never_logged(app_user, make_agent, capsys) -> None:  # type: ignore[no-untyped-def]
    c = make_agent("agent:f")
    key = c.headers["Authorization"].split()[1]
    c.get(f"{B}/stats")
    c.get(f"{B}/stats", headers={"Authorization": "Bearer ame_agt_" + "y" * 43})
    out = capsys.readouterr()
    user_tok = app_user.headers["Authorization"].split()[1]
    for secret in (key, user_tok):
        assert secret not in out.out and secret not in out.err

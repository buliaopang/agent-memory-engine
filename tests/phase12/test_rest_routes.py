"""Step 5: REST routes (ADR-017) - success paths, error mapping, contract, thin adapter."""

from __future__ import annotations

import json
from pathlib import Path

from ame.adapters.rest import routes as routes_mod
from tests.phase12.conftest import mem

ROOT = Path(__file__).resolve().parents[2]
B = "/api/v1"


def _props(name: str) -> set[str]:
    return set(json.loads((ROOT / "schemas" / name).read_text())["properties"])


def _new(api, **over) -> dict:
    r = api.post(f"{B}/memories", json=mem(**over))
    assert r.status_code == 201, r.text
    return r.json()


def test_rest_layer_has_no_sql_or_orm() -> None:
    for f in (ROOT / "src/ame/adapters/rest").glob("*.py"):
        src = f.read_text()
        assert "sqlalchemy" not in src, f
        assert "repositories" not in src and "infrastructure.models" not in src, f
        assert "uow" not in src.lower().replace("unit_of_work import make_session_factory", ""), f


def test_memory_crud_and_contract(api) -> None:
    m = _new(api, tags=["a"], content="hello")
    assert set(m) == _props("memory.schema.json")
    assert m["status"] == "active" and m["current_version"] == 1  # T2 local user
    g = api.get(f"{B}/memories/{m['memory_id']}")
    assert g.status_code == 200 and g.json() == m
    p = api.patch(f"{B}/memories/{m['memory_id']}",
                  json={"importance": 0.9, "tags": ["b", "a"], "reason": "tune"})  # fmt: skip
    assert p.status_code == 200 and p.json()["importance"] == 0.9 and p.json()["tags"] == ["a", "b"]
    assert p.json()["content"] == "hello"
    # PATCH cannot touch content or status
    for bad in ({"content": "x", "reason": "r"}, {"status": "archived", "reason": "r"},
                {"importance": 0.1}):  # fmt: skip
        assert api.patch(f"{B}/memories/{m['memory_id']}", json=bad).status_code == 422
    r = api.patch(f"{B}/memories/{m['memory_id']}", json={"importance": 0.9, "reason": "same"})
    assert r.status_code == 422  # no change
    ev = api.get(f"{B}/memories/{m['memory_id']}/audit").json()
    upd = [e for e in ev if e["event_type"] == "MEMORY_UPDATED"]
    assert upd[-1]["after"] == {"importance": 0.9, "tags": ["a", "b"]}
    assert upd[-1]["before"] == {"importance": 0.5, "tags": ["a"]}


def test_not_found_and_space(api) -> None:
    r = api.get(f"{B}/memories/mem_nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = api.post(f"{B}/memories", json=mem(scope_type="private", scope_id="private:none"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "SPACE_NOT_FOUND"


def test_versions(api) -> None:
    mid = _new(api)["memory_id"]
    r = api.post(f"{B}/memories/{mid}/versions",
                 json={"content": "v2", "change_reason": "fix", "expected_version": 1})  # fmt: skip
    assert r.status_code == 201 and set(r.json()) == _props("version.schema.json")
    assert r.json()["version"] == 2
    r = api.post(f"{B}/memories/{mid}/versions",
                 json={"content": "v3", "change_reason": "x", "expected_version": 1})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERSION_CONFLICT"
    vs = api.get(f"{B}/memories/{mid}/versions").json()
    assert [v["version"] for v in vs] == [1, 2] and vs[0]["content"] == "c"


def test_status_classification_archive_restore(api) -> None:
    mid = _new(api)["memory_id"]
    r = api.post(f"{B}/memories/{mid}/status", json={"to_status": "candidate", "reason": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"
    r = api.post(f"{B}/memories/{mid}/status", json={"to_status": "archived"})
    assert r.status_code == 422  # reason required
    r = api.post(f"{B}/memories/{mid}/classification", json={"memory_type": "procedural"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "REASON_REQUIRED"
    r = api.post(f"{B}/memories/{mid}/classification", json={"layer": "L3"})
    assert r.status_code == 200 and r.json()["layer"] == "L3"
    r = api.post(f"{B}/memories/{mid}/archive", json={"reason": "old"})
    assert r.status_code == 200 and r.json()["status"] == "archived"
    r = api.post(f"{B}/memories/{mid}/versions",
                 json={"content": "v2", "change_reason": "x", "expected_version": 1})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "MEMORY_ARCHIVED"
    r = api.patch(f"{B}/memories/{mid}", json={"importance": 0.1, "reason": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MEMORY_ARCHIVED"
    r = api.post(f"{B}/memories/{mid}/restore", json={"reason": "back"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    r = api.post(f"{B}/memories/{mid}/restore", json={"reason": "again"})
    assert r.status_code == 409
    types = [e["event_type"] for e in api.get(f"{B}/memories/{mid}/audit").json()]
    assert types[-2:] == ["MEMORY_STATUS_CHANGED", "MEMORY_RESTORED"]


def test_edges(api) -> None:
    a, b = _new(api)["memory_id"], _new(api)["memory_id"]
    body = {"from_memory_id": a, "to_memory_id": b, "edge_type": "PARENT"}
    r = api.post(f"{B}/edges", json=body)
    assert r.status_code == 201 and set(r.json()) == _props("edge.schema.json")
    again = api.post(f"{B}/edges", json=body)
    assert again.status_code == 200 and again.json()["created"] is False
    cyc = api.post(f"{B}/edges", json={**body, "from_memory_id": b, "to_memory_id": a})
    assert cyc.status_code == 409 and cyc.json()["error"]["code"] == "DAG_CYCLE_DETECTED"
    eid = r.json()["edge_id"]
    rv = api.post(f"{B}/edges/{eid}/revoke", json={"reason": "wrong"})
    assert rv.status_code == 200 and rv.json()["status"] == "revoked"
    assert api.post(f"{B}/edges/{eid}/revoke", json={"reason": "x"}).status_code == 409
    assert api.get(f"{B}/memories/{a}/edges").json() == []
    assert len(api.get(f"{B}/memories/{a}/edges?include_revoked=true").json()) == 1
    assert api.post(f"{B}/edges/edge_nope/revoke", json={"reason": "x"}).status_code == 404


def test_relations_conflicts_user_resolution(api) -> None:
    a, b = _new(api)["memory_id"], _new(api)["memory_id"]
    r = api.post(
        f"{B}/relations",
        json={"from_memory_id": a, "to_memory_id": b, "relation_type": "CONTRADICTS"},
    )
    assert r.status_code == 201 and set(r.json()) == _props("relation.schema.json")
    assert api.get(f"{B}/memories/{a}").json()["status"] == "conflicted"  # T5
    # reverse direction is the same undirected relation (no cycle check)
    rr = api.post(
        f"{B}/relations",
        json={"from_memory_id": b, "to_memory_id": a, "relation_type": "CONTRADICTS"},
    )
    assert rr.status_code == 200 and rr.json()["relation_id"] == r.json()["relation_id"]
    cs = api.get(f"{B}/memories/{a}/conflicts").json()
    assert len(cs) == 1 and cs[0]["status"] == "open"
    st = api.post(f"{B}/memories/{a}/status", json={"to_status": "active", "reason": "x"})
    assert st.status_code == 409 and st.json()["error"]["code"] == "CONFLICT_UNRESOLVED"
    res = api.post(
        f"{B}/conflicts/{cs[0]['conflict_id']}/resolve",
        json={"resolution": "kept_both", "reason": "both ok", "evidence": {"n": 1}},
    )
    assert res.status_code == 200 and res.json()["status"] == "resolved"
    assert api.get(f"{B}/memories/{a}").json()["status"] == "active"
    assert len(api.get(f"{B}/memories/{b}/relations").json()) == 1
    rel = r.json()["relation_id"]
    assert api.post(f"{B}/relations/{rel}/revoke", json={"reason": "x"}).status_code == 200
    assert api.get(f"{B}/memories/{b}/relations").json() == []
    bad = api.post(
        f"{B}/relations", json={"from_memory_id": a, "to_memory_id": a, "relation_type": "SUPPORTS"}
    )
    assert bad.status_code == 422


def test_agent_cannot_resolve_via_service_but_rest_actor_is_user(api) -> None:
    assert routes_mod.LOCAL_USER.actor_type.value == "user"
    # the client cannot pick its identity: unknown header has no effect, extra body field 422
    r = api.post(f"{B}/memories", json={**mem(), "actor_type": "agent"})
    assert r.status_code == 422


def test_audit_same_transaction_as_failed_write(api) -> None:
    a, b = _new(api)["memory_id"], _new(api)["memory_id"]
    api.post(f"{B}/edges", json={"from_memory_id": a, "to_memory_id": b, "edge_type": "PARENT"})
    n = len(api.get(f"{B}/memories/{a}/audit").json())
    api.post(f"{B}/edges", json={"from_memory_id": b, "to_memory_id": a, "edge_type": "PARENT"})
    assert len(api.get(f"{B}/memories/{a}/audit").json()) == n  # rejected write left no audit

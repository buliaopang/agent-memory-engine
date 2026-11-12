"""Step 6: Phase 1 acceptance (ROADMAP): 1000 nodes, restart integrity, DAG rules, 6 states."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from ame.adapters.rest.app import create_app
from ame.application.integrity_service import IntegrityService
from tests.conftest import AGENT, make, read_user_token
from tests.phase12.conftest import mem

B = "/api/v1"


def _client(settings) -> TestClient:
    c = TestClient(create_app(settings))
    c.headers["Authorization"] = f"Bearer {read_user_token(settings.data_dir)}"
    return c


def test_1000_nodes_write_read_and_restart(settings) -> None:
    t0 = time.perf_counter()
    ids: list[str] = []
    with _client(settings) as c:
        for i in range(1000):
            parents = [ids[(i - 1) // 2]] if i else []  # binary-tree shaped DAG via PARENT edges
            r = c.post(f"{B}/memories", json=mem(content=f"node-{i}", parent_ids=parents))
            assert r.status_code == 201
            ids.append(r.json()["memory_id"])
        for i in (0, 1, 499, 999):
            g = c.get(f"{B}/memories/{ids[i]}").json()
            assert g["content"] == f"node-{i}"
        v = c.post(f"{B}/memories/{ids[5]}/versions",
                   json={"content": "v2", "change_reason": "r", "expected_version": 1})  # fmt: skip
        assert v.status_code == 201
        snapshot = {i: c.get(f"{B}/memories/{ids[i]}").json() for i in (0, 5, 777, 999)}
        audit5 = c.get(f"{B}/memories/{ids[5]}/audit").json()
    elapsed = time.perf_counter() - t0
    # restart: brand-new app/engine on the same data directory
    with _client(settings) as c2:
        for i, before in snapshot.items():
            assert c2.get(f"{B}/memories/{ids[i]}").json() == before
        assert [x["version"] for x in c2.get(f"{B}/memories/{ids[5]}/versions").json()] == [1, 2]
        assert c2.get(f"{B}/memories/{ids[5]}/audit").json() == audit5
        assert len(c2.get(f"{B}/memories/{ids[0]}/edges").json()) == 2
    rep = IntegrityService(settings.db_path).run()
    assert rep.exit_code == 0, rep.to_dict()
    print(f"\n[acceptance] 1000 REST writes + reads: {elapsed:.2f}s")


def test_illegal_cycle_rejected_contradicts_not_in_cycle_check(settings) -> None:
    with _client(settings) as c:
        a, b, x = (c.post(f"{B}/memories", json=mem()).json()["memory_id"] for _ in range(3))
        assert (
            c.post(
                f"{B}/edges",
                json={"from_memory_id": a, "to_memory_id": b, "edge_type": "DERIVED_FROM"},
            ).status_code
            == 201
        )
        assert (
            c.post(
                f"{B}/edges",
                json={"from_memory_id": b, "to_memory_id": x, "edge_type": "REFERENCE"},
            ).status_code
            == 201
        )
        r = c.post(f"{B}/edges", json={"from_memory_id": x, "to_memory_id": a,
                                       "edge_type": "PARENT"})  # fmt: skip
        assert r.status_code == 409 and r.json()["error"]["code"] == "DAG_CYCLE_DETECTED"
        for f, t in ((x, a), (a, x)):
            rr = c.post(
                f"{B}/relations",
                json={"from_memory_id": f, "to_memory_id": t, "relation_type": "SUPPORTS"},
            )
            assert rr.status_code == 201  # semantic relations never cycle-checked


def _status(c, mid, to, reason="r", **kw):
    return c.post(f"{B}/memories/{mid}/status", json={"to_status": to, "reason": reason, **kw})


def test_all_transitions_reachable_through_rest(settings) -> None:
    """T2/T5-T11 driven purely by REST; candidates (T1) are written by an agent through the
    same MemoryService (REST cannot claim agent identity before Phase 3), then T3/T4/T12
    are driven by REST."""
    seen: set[str] = set()
    with _client(settings) as c:
        svc = c.app.state.memory_service  # type: ignore[attr-defined]

        def trig(mid: str) -> str:
            return c.get(f"{B}/memories/{mid}/audit").json()[-1]["after"]["trigger"]

        def new() -> str:
            m = c.post(f"{B}/memories", json=mem()).json()
            seen.add(trig(m["memory_id"]))
            return m["memory_id"]

        # Phase 3a: the agent must be registered before it can write (ADR-025)
        assert (
            c.post(
                f"{B}/agents",
                json={"agent_id": AGENT.actor_id, "display_name": "codex", "agent_type": "test"},
            ).status_code
            == 201
        )
        cand = svc.create_memory(make(), AGENT).memory_id
        seen.add(trig(cand))  # T1
        assert _status(c, cand, "active").status_code == 200
        seen.add(trig(cand))  # T3
        cand2 = svc.create_memory(make(), AGENT).memory_id
        assert _status(c, cand2, "rejected").status_code == 200
        seen.add(trig(cand2))  # T4
        assert _status(c, cand2, "candidate").status_code == 200
        seen.add(trig(cand2))  # T12
        a, b = new(), new()  # T2
        rel = c.post(
            f"{B}/relations",
            json={"from_memory_id": a, "to_memory_id": b, "relation_type": "CONTRADICTS"},
        )
        assert rel.status_code == 201
        seen.add(c.get(f"{B}/memories/{a}/audit").json()[-1]["after"].get("trigger", ""))  # T5
        cf = c.get(f"{B}/memories/{a}/conflicts").json()[0]
        lo, hi = cf["memory_a_id"], cf["memory_b_id"]
        res = c.post(
            f"{B}/conflicts/{cf['conflict_id']}/resolve",
            json={"resolution": "a_superseded_by_b", "reason": "r", "evidence": {"k": 1}},
        )
        assert res.status_code == 200
        seen.add(trig(lo))  # T7
        seen.add(trig(hi))  # T6 (winner back to active)
        assert _status(c, lo, "active").status_code == 200
        seen.add(trig(lo))  # T11
        d, e = new(), new()
        assert _status(c, d, "superseded", superseded_by_memory_id=e).status_code == 200
        seen.add(trig(d))  # T8
        assert c.post(f"{B}/memories/{e}/archive", json={"reason": "r"}).status_code == 200
        seen.add(trig(e))  # T9
        assert c.post(f"{B}/memories/{e}/restore", json={"reason": "r"}).status_code == 200
        seen.add(trig(e))  # T10
    assert {f"T{i}" for i in range(1, 13)} <= seen, sorted(seen)

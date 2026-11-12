from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ame.adapters.rest.app import create_app
from ame.config import Settings
from tests.conftest import read_user_token
from tests.phase12.conftest import api, mem  # noqa: F401  (fixture re-export)

SECRET = "机密查询词ZQX"


def test_requires_token(client: TestClient) -> None:
    assert client.post("/api/v1/memories/search", json={"query": "x"}).status_code == 401
    for path in ("/api/v1/graph?space_id=shared", "/api/v1/stats", "/api/v1/memories/m/neighbors"):
        assert client.get(path).status_code == 401


def test_search_neighbors_graph_stats(api: TestClient) -> None:  # noqa: F811
    a = api.post("/api/v1/memories", json=mem(content="中文 搜索 测试 内容")).json()["memory_id"]
    b = api.post("/api/v1/memories", json=mem(content="另一条")).json()["memory_id"]
    api.post("/api/v1/edges", json={"from_memory_id": a, "to_memory_id": b,
                                     "edge_type": "REFERENCE"})  # fmt: skip
    r = api.post("/api/v1/memories/search", json={"query": "搜索"})
    assert r.status_code == 200
    body = r.json()
    assert body["retrieval_mode"] == "lexical_graph"
    assert body["results"][0]["memory"]["memory_id"] == a
    assert body["results"][0]["score_breakdown"]["vector"] is None
    assert body["results"][0]["memory"]["vector_status"] == "pending"
    n = api.get(f"/api/v1/memories/{a}/neighbors").json()
    assert {x["memory_id"] for x in n["nodes"]} == {a, b}
    assert api.get(f"/api/v1/memories/{a}/neighbors?direction=in").json()["links"] == []
    assert api.get(f"/api/v1/memories/{a}/neighbors?depth=9").status_code == 422
    assert api.get("/api/v1/memories/mem_x/neighbors").status_code == 404
    g = api.get("/api/v1/graph?space_id=shared&limit=1").json()
    assert g["truncated"] and len(g["nodes"]) == 1
    assert api.get("/api/v1/graph").status_code == 422
    s = api.get("/api/v1/stats").json()
    assert s["memories"]["total"] == 2 and s["retrieval_mode"] == "lexical_graph"
    assert s["outbox"]["pending"] >= 2


def test_422_does_not_echo_and_query_not_logged(
    api: TestClient,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = api.post("/api/v1/memories/search", json={"query": SECRET, "top_k": 999})
    assert bad.status_code == 422 and SECRET not in bad.text
    ok = api.post("/api/v1/memories/search", json={"query": SECRET})
    assert ok.status_code == 200 and SECRET not in ok.text  # query is not echoed
    out = capsys.readouterr()
    assert SECRET not in caplog.text and SECRET not in out.out and SECRET not in out.err


@pytest.fixture()
def live(tmp_path) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    s = Settings(data_dir=tmp_path / "d", index_worker_enabled=True, index_worker_interval_s=0.05)
    with TestClient(create_app(s)) as c:
        c.headers["Authorization"] = f"Bearer {read_user_token(s.data_dir)}"
        yield c


def test_background_worker_drains_outbox(live: TestClient) -> None:
    m = live.post("/api/v1/memories", json=mem(content="bg")).json()["memory_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        h = live.get("/api/v1/health").json()
        if h["worker"]["pending_events"] == 0:
            break
        time.sleep(0.05)
    assert h["worker"]["status"] == "running" and h["worker"]["pending_events"] == 0
    assert live.get(f"/api/v1/memories/{m}").json()["vector_status"] == "disabled"

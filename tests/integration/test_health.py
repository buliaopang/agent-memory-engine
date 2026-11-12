def test_health_ok(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sqlite"]["status"] == "ok"
    assert body["sqlite"]["journal_mode"] == "wal"
    assert body["vector_store"]["status"] == "not_configured"
    # Phase 2: worker is disabled in the test fixture; mode is lexical_graph (no provider)
    assert body["worker"]["status"] == "disabled"
    assert body["vector_store"]["retrieval_mode"] == "lexical_graph"
    assert "X-Request-ID" in r.headers


def test_health_does_not_leak_paths(client, settings):
    assert str(settings.data_dir) not in client.get("/api/v1/health").text


def test_unknown_route_404(client):
    assert client.get("/api/v1/nope").status_code == 404

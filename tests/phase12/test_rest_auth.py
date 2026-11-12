"""Step 1: token lock, error envelope, Idempotency-Key refusal (ADR-017)."""

from __future__ import annotations

import logging
import os
import stat

import pytest
from fastapi.testclient import TestClient

from ame.adapters.rest.app import create_app
from ame.infrastructure.token_store import load_or_create_token, token_matches, token_path
from tests.conftest import read_user_token
from tests.phase12.conftest import mem


def test_token_generated_once_and_reused(settings) -> None:
    t1 = load_or_create_token(settings.data_dir)
    t2 = load_or_create_token(settings.data_dir)
    assert t1 == t2 and len(t1) >= 32


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode; Windows ACL is Q15 / Phase 3")
def test_token_file_mode_0600(settings) -> None:
    load_or_create_token(settings.data_dir)
    assert stat.S_IMODE(token_path(settings.data_dir).stat().st_mode) == 0o600


def test_invalid_token_file_rejected(settings) -> None:
    settings.data_dir.mkdir(parents=True)
    token_path(settings.data_dir).write_text("short")
    with pytest.raises(ValueError):
        load_or_create_token(settings.data_dir)


def test_token_matches() -> None:
    assert token_matches("a" * 40, "a" * 40)
    assert not token_matches("a" * 40, "b" * 40)
    assert not token_matches("a" * 40, None)


def test_auth_required(settings) -> None:
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/v1/health").status_code == 200  # health stays open
        r = c.post("/api/v1/memories", json=mem())
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
        r = c.get("/api/v1/memories/x", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        r = c.get("/api/v1/memories/x", headers={"Authorization": "Basic abc"})
        assert r.status_code == 401


def test_token_never_logged_or_echoed(settings, caplog, capsys) -> None:
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(settings)) as c:
        tok = read_user_token(settings.data_dir)
        c.post("/api/v1/memories", json=mem(), headers={"Authorization": f"Bearer {tok}"})
        r = c.get("/api/v1/memories/x", headers={"Authorization": "Bearer guess-value-123"})
        assert "guess-value-123" not in r.text
    out = capsys.readouterr()
    assert tok not in caplog.text and tok not in out.out and tok not in out.err


def test_idempotency_key_explicitly_not_supported(api) -> None:
    # Phase 3b (ADR-027) replaced the 501 refusal with real support; the key is never "ignored
    # silently": the first call writes, the repeat returns the stored response, marked replayed.
    r = api.post("/api/v1/memories", json=mem(), headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    r2 = api.post("/api/v1/memories", json=mem(), headers={"Idempotency-Key": "k1"})
    assert r2.status_code == 201 and r2.headers["Idempotency-Replayed"] == "true"
    assert r2.json() == r.json()
    assert api.get("/api/v1/memories/none").status_code == 404  # nothing was written path-wise


def test_validation_error_envelope_does_not_echo_content(api) -> None:
    r = api.post("/api/v1/memories", json=mem(importance=5, content="PRIVATE-TEXT"))
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR" and "PRIVATE-TEXT" not in r.text
    assert body["request_id"].startswith("req_")


def test_unknown_route_and_method_envelope(api) -> None:
    r = api.get("/api/v1/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = api.delete("/api/v1/memories/x")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"

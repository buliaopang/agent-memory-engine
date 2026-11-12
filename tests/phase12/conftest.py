from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ame.adapters.rest.app import create_app
from ame.config import Settings
from tests.conftest import read_user_token


@pytest.fixture()
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        tok = read_user_token(settings.data_dir)
        c.headers["Authorization"] = f"Bearer {tok}"
        yield c


def mem(**over: object) -> dict[str, object]:
    d: dict[str, object] = {
        "content": "c", "memory_type": "semantic", "layer": "L2", "scope_type": "shared",
        "scope_id": "shared", "importance": 0.5, "confidence": 0.5,
    }  # fmt: skip
    d.update(over)
    return d

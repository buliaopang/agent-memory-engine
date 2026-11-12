from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from ame.application.search_service import SearchService
from ame.infrastructure.unit_of_work import make_session_factory

NOW = datetime(2026, 11, 3, tzinfo=UTC)


@pytest.fixture()
def search(engine: Engine) -> SearchService:
    return SearchService(make_session_factory(engine))

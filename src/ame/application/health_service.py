"""Health use case shared by REST (and later MCP) adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ame import __version__


class HealthProbe(Protocol):
    def check(self) -> dict[str, Any]: ...


class HealthService:
    def __init__(
        self,
        db_probe: HealthProbe,
        index_probe: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._db = db_probe
        self._index = index_probe

    def get_health(self) -> dict[str, Any]:
        db = self._db.check()
        return {
            "status": "ok" if db["status"] == "ok" else "degraded",
            "service": {"name": "ame", "version": __version__},
            "sqlite": db,
            **(
                self._index()
                if self._index
                else {
                    "vector_store": {"status": "not_configured"},
                    "worker": {"status": "not_running"},
                }
            ),
        }

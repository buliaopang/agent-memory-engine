"""Read-only topological order for `ame graph-order` (ADR-019).

Uses the read-only IntegrityRepository connection (mode=ro + query_only), so it can never
change the database. Output contains memory_ids only - never memory content.
"""

from __future__ import annotations

from pathlib import Path

from ame.application.schemas import GraphOrder
from ame.domain.enums import EdgeType
from ame.domain.topology import topological_order
from ame.errors import NotFoundError, ValidationFailedError
from ame.infrastructure.integrity_repository import IntegrityRepository

MAX_LIMIT = 100_000
DEFAULT_LIMIT = 1000


class TopologyService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def order(
        self, edge_types: list[EdgeType] | None = None, limit: int = DEFAULT_LIMIT
    ) -> GraphOrder:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValidationFailedError(f"limit must be 1..{MAX_LIMIT}", {"limit": limit})
        if not self.db_path.exists():
            raise NotFoundError("database file not found")
        types = edge_types or list(EdgeType)
        want = {t.value for t in types}
        repo = IntegrityRepository(self.db_path)
        try:
            nodes = repo.memory_ids()
            edges = [(a, b) for _e, a, b, t in repo.active_structural_edges() if t in want]
        finally:
            repo.close()
        ordered = topological_order(nodes, edges)
        return GraphOrder(
            memory_ids=ordered[:limit],
            node_count=len(ordered),
            edge_count=len(edges),
            truncated=len(ordered) > limit,
            edge_types=sorted(types),
        )

"""Read-only graph expansion: neighbours and bounded sub-graphs (API_SPEC neighbors / graph).
Returns ids, types, status and summaries only - never full content. Node counts are capped."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from sqlalchemy.orm import Session, sessionmaker

from ame.application.access import resolve
from ame.application.schemas import Actor, GraphLink, GraphNode, GraphView
from ame.errors import NotFoundError, ValidationFailedError
from ame.infrastructure.search_repository import STRUCTURAL
from ame.infrastructure.unit_of_work import UnitOfWork

RELATIONS = ("CONTRADICTS", "SUPPORTS", "RELATED_TO")
MAX_DEPTH = 3
MAX_NEIGHBORS = 200
MAX_GRAPH_NODES = 500


class GraphQueryService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def neighbors(
        self,
        memory_id: str,
        edge_types: Sequence[str] = STRUCTURAL,
        relation_types: Sequence[str] = RELATIONS,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        actor: Actor | None = None,
    ) -> GraphView:
        if direction not in ("in", "out", "both"):
            raise ValidationFailedError("direction must be in, out or both")
        if not 1 <= depth <= MAX_DEPTH or not 1 <= limit <= MAX_NEIGHBORS:
            raise ValidationFailedError(
                "depth or limit out of range", {"max_depth": MAX_DEPTH, "max_limit": MAX_NEIGHBORS}
            )
        bad = [t for t in edge_types if t not in STRUCTURAL]
        bad += [t for t in relation_types if t not in RELATIONS]
        if bad:
            raise ValidationFailedError("unknown link type", {"types": bad})
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            acc.see_memory(uow.memories.get(memory_id), memory_id)
            seen = {memory_id: 0}
            links: dict[str, GraphLink] = {}
            truncated = False
            queue = deque([memory_id])
            while queue:
                cur = queue.popleft()
                if seen[cur] >= depth:
                    continue
                for kind, lid, ltype, frm, to in uow.search.neighbor_links(
                    cur, edge_types, relation_types
                ):
                    undirected = kind == "relation" and ltype != "SUPPORTS"
                    if not undirected:
                        if direction == "out" and frm != cur:
                            continue
                        if direction == "in" and to != cur:
                            continue
                    other = to if frm == cur else frm
                    if acc.readable is not None and other not in seen:
                        om = uow.memories.get(other)
                        if om is None or not acc.can_read(om.scope_id):
                            continue  # invisible neighbour: neither traversed nor shown
                    if other not in seen:
                        if len(seen) - 1 >= limit:
                            truncated = True
                            continue
                        seen[other] = seen[cur] + 1
                        queue.append(other)
                    links[lid] = GraphLink(
                        kind=kind,  # type: ignore[arg-type]
                        link_id=lid,
                        link_type=ltype,
                        from_memory_id=frm,
                        to_memory_id=to,
                    )
            return self._view(uow, memory_id, seen, list(links.values()), truncated)

    def graph(
        self, space_id: str | None = None, center_id: str | None = None, depth: int = 1,
        limit: int = 200, actor: Actor | None = None,
    ) -> GraphView:  # fmt: skip
        if not 1 <= limit <= MAX_GRAPH_NODES:
            raise ValidationFailedError("limit out of range", {"max_limit": MAX_GRAPH_NODES})
        if center_id is not None:
            return self.neighbors(
                center_id, depth=depth, limit=min(limit, MAX_NEIGHBORS), actor=actor
            )
        if space_id is None:
            raise ValidationFailedError("space_id or center_id is required")
        with UnitOfWork(self._sf, read_only=True) as uow:
            if not resolve(uow, actor).can_read(space_id):
                raise NotFoundError("Space not found", {"space_id": space_id})
            ids = uow.search.space_memory_ids(space_id, limit + 1)
            truncated = len(ids) > limit
            ids = ids[:limit]
            links = [
                GraphLink(
                    kind=k,  # type: ignore[arg-type]
                    link_id=lid,
                    link_type=t,
                    from_memory_id=a,
                    to_memory_id=b,
                )
                for k, lid, t, a, b in uow.search.all_structural_links(ids)
            ]
            return self._view(uow, None, dict.fromkeys(ids, 0), links, truncated)

    @staticmethod
    def _view(
        uow: UnitOfWork,
        center: str | None,
        depths: dict[str, int],
        links: list[GraphLink],
        truncated: bool,
    ) -> GraphView:
        heads = uow.search.node_headers(depths)
        nodes = [
            GraphNode.model_validate(
                {
                    "memory_id": mid,
                    "memory_type": h[0],
                    "layer": h[1],
                    "scope_id": h[2],
                    "status": h[3],
                    "summary": h[4] or None,
                    "depth": depths[mid],
                }
            )
            for mid, h in sorted(heads.items(), key=lambda kv: (depths[kv[0]], kv[0]))
        ]
        keep = set(heads)
        links = [x for x in links if x.from_memory_id in keep and x.to_memory_id in keep]
        links.sort(key=lambda x: x.link_id)
        return GraphView(center_id=center, nodes=nodes, links=links, truncated=truncated)

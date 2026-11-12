"""SearchService (ADR-020): filter -> recall (lexical + optional vector) -> rank -> results.

- Filters (scope/layer/type/status) run BEFORE recall (P2-7). Phase 3a (ADR-025): the
  requested scope_ids are first intersected with the caller's readable spaces, so invisible
  memories never enter recall, ranking, graph context or counts.
- Default excludes archived and rejected; conflicted only with include_conflicts=true.
- Without a working vector backend: retrieval_mode = lexical_graph, vector score = None.
- Never logs the query or memory text.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from ame.application.access import UNRESTRICTED, Access, resolve
from ame.application.ids import new_id
from ame.application.ports import EmbeddingProvider, VectorStore, VectorUnavailableError
from ame.application.schemas import (
    Actor,
    GraphContextItem,
    ScoreBreakdown,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from ame.domain.enums import MemoryStatus
from ame.domain.ranking import (
    Components,
    Weights,
    freshness_score,
    graph_score,
    keyword_score,
    query_terms,
    total_score,
    weights_used,
)
from ame.infrastructure.models import Memory
from ame.infrastructure.search_repository import SEMANTIC_LINKS, STRUCTURAL
from ame.infrastructure.unit_of_work import UnitOfWork

HIDDEN = {MemoryStatus.ARCHIVED.value, MemoryStatus.REJECTED.value}
VECTOR_RECALL_MAX = 200
GRAPH_CONTEXT_MAX = 10


@dataclass(frozen=True)
class VectorBackend:
    provider: EmbeddingProvider
    store: VectorStore


class SearchService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        weights: Weights | None = None,
        half_life_days: float = 30.0,
        vector: VectorBackend | None = None,
    ) -> None:
        self._sf = session_factory
        self._w = weights or Weights()
        self._half = half_life_days
        self._vector = vector

    def _vector_hits(
        self, query: str, allowed: set[str], k: int
    ) -> tuple[dict[str, tuple[int, str, float]], str | None]:
        if self._vector is None:
            return {}, "no embedding provider configured"
        try:
            qv = self._vector.provider.embed([query])[0]
            hits = self._vector.store.search(qv, k, allowed)
        except VectorUnavailableError as exc:
            return {}, f"vector backend unavailable: {type(exc).__name__}"
        except Exception as exc:  # degrade, never fail the search on the derived index
            return {}, f"vector backend error: {type(exc).__name__}"
        return {h.memory_id: (h.version, h.embedding_model, h.similarity) for h in hits}, None

    def search(
        self, req: SearchRequest, now: datetime | None = None, actor: Actor | None = None
    ) -> SearchResponse:
        from ame.application.memory_service import MemoryService  # avoid import cycle

        now = now or datetime.now(UTC)
        statuses = [s.value for s in MemoryStatus if s.value not in HIDDEN]
        if not req.include_conflicts:
            statuses.remove(MemoryStatus.CONFLICTED.value)
        terms = query_terms(req.query)
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            scopes = list(req.scope_ids)
            if acc.readable is not None:
                scopes = (
                    [x for x in scopes if x in acc.readable] if scopes else sorted(acc.readable)
                )
            allowed = (
                set()
                if acc.readable is not None and not scopes
                else uow.search.allowed_ids(
                    scopes,
                    [x.value for x in req.layers],
                    [x.value for x in req.memory_types],
                    statuses,
                )
            )
            if req.epoch_id is not None:  # Phase 4 (ADR-030): only active epoch members
                from ame.application.phase4_service import epoch_filter

                allowed = allowed & (epoch_filter(uow, acc, req.epoch_id) or set())
            matched: dict[str, int] = defaultdict(int)
            for t in terms:
                for mid in uow.search.term_hits(t) & allowed:
                    matched[mid] += 1
            vec, degraded = self._vector_hits(
                req.query, allowed, min(VECTOR_RECALL_MAX, req.top_k * 5)
            )
            mode = "lexical_graph" if self._vector is None or degraded else "hybrid"
            candidates = (set(matched) | set(vec)) & allowed
            neighbours: dict[str, set[str]] = defaultdict(set)
            for a, b in uow.search.links_among(candidates):
                if a != b:
                    neighbours[a].add(b)
                    neighbours[b].add(a)
            scored: list[tuple[float, str, Components, Memory]] = []
            for mid in candidates:
                m = uow.memories.get(mid)
                if m is None:
                    continue
                sim: float | None = None
                if mid in vec:
                    version, model, s = vec[mid]
                    # stale vectors (older version or other model) are not used
                    if version == m.current_version and model == self._model():
                        sim = s
                if sim is None and matched.get(mid, 0) == 0:
                    continue  # only a stale vector hit: nothing truthful to rank it by
                comp = Components(
                    keyword=keyword_score(matched.get(mid, 0), len(terms)),
                    vector=sim,
                    graph=graph_score(len(neighbours[mid])),
                    importance=m.importance,
                    confidence=m.confidence,
                    freshness=freshness_score(m.updated_at, now, self._half),
                )
                scored.append((total_score(comp, self._w), mid, comp, m))
            scored.sort(key=lambda x: (-x[0], x[1]))
            results = []
            for score, mid, comp, m in scored[: req.top_k]:
                read = MemoryService._read(uow, m, acc)
                if not req.include_sources:
                    read = read.model_copy(update={"sources": []})
                ctx = self._graph_context(uow, mid, acc) if req.include_graph_context else []
                results.append(
                    SearchResult(
                        memory=read,
                        score=score,
                        score_breakdown=ScoreBreakdown(
                            vector=None if comp.vector is None else round(comp.vector, 6),
                            keyword=round(comp.keyword, 6),
                            graph=round(comp.graph, 6),
                            importance=comp.importance,
                            confidence=comp.confidence,
                            freshness=round(comp.freshness, 6),
                            weights=weights_used(comp, self._w),
                            vector_used=comp.vector is not None,
                        ),
                        matched_terms=matched.get(mid, 0),
                        conflicted=read.status == MemoryStatus.CONFLICTED,
                        graph_context=ctx,
                    )
                )
        return SearchResponse(
            query_id=new_id("qry"),
            retrieval_mode=mode,  # type: ignore[arg-type]
            degraded_reason=degraded if mode == "lexical_graph" else None,
            total_candidates=len(scored),
            results=results,
        )

    def _model(self) -> str | None:
        return self._vector.provider.model_name if self._vector else None

    @staticmethod
    def _graph_context(
        uow: UnitOfWork, mid: str, acc: Access = UNRESTRICTED
    ) -> list[GraphContextItem]:
        links = uow.search.neighbor_links(mid, STRUCTURAL, (*SEMANTIC_LINKS, "CONTRADICTS"))
        out: list[GraphContextItem] = []
        for kind, _lid, ltype, frm, to in links:
            if len(out) >= GRAPH_CONTEXT_MAX:
                break
            other, direction = (to, "out") if frm == mid else (frm, "in")
            if acc.readable is not None:
                om = uow.memories.get(other)
                if om is None or not acc.can_read(om.scope_id):
                    continue
            out.append(
                GraphContextItem(
                    memory_id=other,
                    kind=kind,  # type: ignore[arg-type]
                    link_type=ltype,
                    direction=direction,  # type: ignore[arg-type]
                )
            )
        return out

    def stats(self, actor: Actor | None = None) -> dict[str, int]:
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            if acc.readable is not None:
                c = uow.search.status_counts_in(sorted(acc.readable))
            else:
                c = uow.search.counts("SELECT status, COUNT(*) FROM memories GROUP BY status")
        c["total"] = sum(c.values())
        return c

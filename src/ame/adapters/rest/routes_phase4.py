"""REST v1 Phase 4 routes (ADR-029..032). Thin adapter: parse -> service -> serialize.
Every POST honours Idempotency-Key through the app middleware (ADR-027)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from ame.adapters.rest.routes import ActorDep, require_token
from ame.application.graph_export_service import GraphExportService
from ame.application.phase4_service import (
    DecayService,
    DistillService,
    EpochService,
    FeedbackService,
    ReviewService,
    SpineService,
)
from ame.application.schemas import (
    Actor,
    DecayArchive,
    DecayResponse,
    DerivedEpochRead,
    DistillRequest,
    DistillResult,
    EpochClose,
    EpochCreate,
    EpochMemberChange,
    EpochRead,
    FeedbackCreate,
    FeedbackRead,
    FeedbackSummary,
    MemoryRead,
    PromoteRequest,
    ReviewItem,
    SpineResponse,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])


def _fb(request: Request) -> FeedbackService:
    s: FeedbackService = request.app.state.feedback_service
    return s


def _ep(request: Request) -> EpochService:
    s: EpochService = request.app.state.epoch_service
    return s


def _sp(request: Request) -> SpineService:
    s: SpineService = request.app.state.spine_service
    return s


def _dc(request: Request) -> DecayService:
    s: DecayService = request.app.state.decay_service
    return s


def _ds(request: Request) -> DistillService:
    s: DistillService = request.app.state.distill_service
    return s


def _rv(request: Request) -> ReviewService:
    s: ReviewService = request.app.state.review_service
    return s


def _gx(request: Request) -> GraphExportService:
    s: GraphExportService = request.app.state.graph_export_service
    return s


FbDep, EpDep, SpDep = Depends(_fb), Depends(_ep), Depends(_sp)
DcDep, DsDep, RvDep, GxDep = Depends(_dc), Depends(_ds), Depends(_rv), Depends(_gx)


@router.post("/memories/{memory_id}/feedback", status_code=201)
def add_feedback(
    memory_id: str, body: FeedbackCreate, s: FeedbackService = FbDep, a: Actor = ActorDep
) -> FeedbackRead:
    return s.add(memory_id, body, a)


@router.get("/memories/{memory_id}/feedback")
def feedback_summary(
    memory_id: str, s: FeedbackService = FbDep, a: Actor = ActorDep
) -> FeedbackSummary:
    return s.summary(memory_id, a)


@router.get("/epochs")
def list_epochs(s: EpochService = EpDep, a: Actor = ActorDep) -> list[EpochRead]:
    return s.list(a)


@router.post("/epochs", status_code=201)
def create_epoch(body: EpochCreate, s: EpochService = EpDep, a: Actor = ActorDep) -> EpochRead:
    return s.create(body, a)


@router.get("/epochs/derived")
def derived_epochs(
    scope_id: str | None = None, s: SpineService = SpDep, a: Actor = ActorDep
) -> list[DerivedEpochRead]:
    return s.spine(scope_id, 200, None, a).derived_epochs


@router.get("/epochs/{epoch_id}")
def get_epoch(epoch_id: str, s: EpochService = EpDep, a: Actor = ActorDep) -> EpochRead:
    return s.get(epoch_id, a)


@router.post("/epochs/{epoch_id}/close")
def close_epoch(
    epoch_id: str, body: EpochClose, s: EpochService = EpDep, a: Actor = ActorDep
) -> EpochRead:
    return s.close(epoch_id, body, a)


@router.post("/epochs/{epoch_id}/members")
def add_epoch_member(
    epoch_id: str, body: EpochMemberChange, s: EpochService = EpDep, a: Actor = ActorDep
) -> EpochRead:
    return s.add_member(epoch_id, body, a)


@router.post("/epochs/{epoch_id}/members/remove")
def remove_epoch_member(
    epoch_id: str, body: EpochMemberChange, s: EpochService = EpDep, a: Actor = ActorDep
) -> EpochRead:
    return s.remove_member(epoch_id, body, a)


@router.get("/spine")
def spine(
    scope_id: str | None = None,
    epoch_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    s: SpineService = SpDep,
    a: Actor = ActorDep,
) -> SpineResponse:
    return s.spine(scope_id, limit, epoch_id, a)


@router.get("/decay/suggestions")
def decay_suggestions(
    scope_id: str | None = None,
    threshold: float | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=500),
    s: DecayService = DcDep,
    a: Actor = ActorDep,
) -> DecayResponse:
    return s.suggest(scope_id, threshold, limit, a)


@router.post("/decay/archive")
def decay_archive(body: DecayArchive, s: DecayService = DcDep, a: Actor = ActorDep) -> MemoryRead:
    return s.archive_suggested(body, a)


@router.post("/distill", status_code=201)
def distill(body: DistillRequest, s: DistillService = DsDep, a: Actor = ActorDep) -> DistillResult:
    return s.run(body, a)


@router.get("/review")
def review_list(
    scope_id: str | None = None, s: ReviewService = RvDep, a: Actor = ActorDep
) -> list[ReviewItem]:
    return s.list(scope_id, a)


@router.post("/memories/{memory_id}/promote")
def promote(
    memory_id: str, body: PromoteRequest, s: ReviewService = RvDep, a: Actor = ActorDep
) -> MemoryRead:
    return s.promote(memory_id, body, a)


@router.post("/memories/{memory_id}/reject")
def reject(
    memory_id: str, body: PromoteRequest, s: ReviewService = RvDep, a: Actor = ActorDep
) -> MemoryRead:
    return s.reject(memory_id, body, a)


@router.get("/graph/export", response_model=None)
def export_graph(
    scope_id: str | None = None,
    format: str = Query(default="json", pattern="^(html|json)$"),
    s: GraphExportService = GxDep,
    a: Actor = ActorDep,
) -> Response | dict[str, Any]:
    data = s.export_data(scope_id=scope_id, actor=a)
    if format == "html":
        return Response(content=s.render_html(data), media_type="text/html; charset=utf-8")
    return data

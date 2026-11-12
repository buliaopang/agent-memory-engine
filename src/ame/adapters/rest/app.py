"""FastAPI REST adapter. No business logic or SQL here."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ame import __version__
from ame.adapters.mcp_constants import MCP_READ_POSTS, MCP_USER_AGENT_PREFIX
from ame.adapters.rest.routes import router
from ame.adapters.rest.routes_phase4 import router as router_phase4
from ame.application.auth_service import AuthService
from ame.application.context_service import ContextService
from ame.application.graph_export_service import GraphExportService
from ame.application.graph_query_service import GraphQueryService
from ame.application.health_service import HealthService
from ame.application.idempotency_service import IdempotencyService, request_fingerprint
from ame.application.index_worker import IndexWorker, IndexWorkerThread
from ame.application.memory_service import MemoryService
from ame.application.phase4_service import (
    DecayService,
    DistillService,
    EpochService,
    FeedbackService,
    ReviewService,
    SpineService,
)
from ame.application.search_service import SearchService
from ame.application.space_service import AgentService, SpaceService
from ame.config import Settings
from ame.errors import AMEError
from ame.infrastructure.database import DatabaseHealthProbe, create_sqlite_engine, run_migrations
from ame.infrastructure.embeddings import build_vector_backend
from ame.infrastructure.token_store import TokenProvider, default_protector
from ame.infrastructure.unit_of_work import make_session_factory
from ame.logging_setup import configure_logging

log = structlog.get_logger()

_IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _bearer(request: Request) -> str:
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    return presented.strip() if scheme.lower() == "bearer" else ""


def _error(
    code: str, message: str, request_id: str, status: int, details: dict[str, Any] | None = None
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details or {},
        }
    }
    return JSONResponse(status_code=status, content=body)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    run_migrations(settings)
    engine = create_sqlite_engine(settings)
    token_provider = TokenProvider(settings.data_dir, default_protector())

    sf = make_session_factory(engine)
    backend = build_vector_backend(settings)
    search = SearchService(
        sf, settings.search_weights(), settings.search_freshness_half_life_days, vector=backend
    )
    worker = IndexWorker(sf, backend=backend)
    worker_thread = IndexWorkerThread(worker, settings.index_worker_interval_s)

    def index_probe() -> dict[str, Any]:
        # counts only; no paths, no text (health is unauthenticated)
        try:
            st = worker.status()
            pending = st["outbox"].get("pending", 0) + st["outbox"].get("processing", 0)
            failed = st["outbox"].get("failed", 0)
        except Exception as exc:
            return {
                "vector_store": {"status": "not_configured", "retrieval_mode": "lexical_graph"},
                "worker": {"status": "error", "error": type(exc).__name__},
            }
        if not settings.index_worker_enabled:
            wstatus = "disabled"
        else:
            wstatus = "running" if worker_thread.alive else "stopped"
        vs = st.get("vector_store")
        vs_status = (
            ("ok" if vs.get("ok") else "error") if isinstance(vs, dict) else "not_configured"
        )
        return {
            "vector_store": {"status": vs_status, "retrieval_mode": st["mode"]},
            "worker": {
                "status": wstatus,
                "pending_events": pending,
                "failed_events": failed,
                "last_error": worker_thread.last_error,
            },
        }

    health = HealthService(DatabaseHealthProbe(engine), index_probe)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        idem.purge_expired()  # ADR-027: drop expired replay records at startup
        if settings.index_worker_enabled:
            worker_thread.start()
        try:
            yield
        finally:
            worker_thread.stop()
            engine.dispose()

    # CORS middleware intentionally not installed (ARCHITECTURE.md §7).
    app = FastAPI(title="Agent Memory Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.token_provider = token_provider
    app.state.auth_service = AuthService(sf, token_provider)
    app.state.space_service = SpaceService(sf)
    app.state.agent_service = AgentService(sf)
    app.state.memory_service = MemoryService(sf, settings.auto_approve_agent_writes)
    app.state.search_service = search
    app.state.context_service = ContextService(search)
    app.state.graph_query_service = GraphQueryService(sf)
    app.state.index_worker = worker
    # Phase 4 (ADR-029..032)
    app.state.feedback_service = FeedbackService(sf)
    app.state.epoch_service = EpochService(sf)
    app.state.spine_service = SpineService(
        sf, settings.spine_weight_values(), settings.spine_freshness_half_life_days
    )
    app.state.decay_service = DecayService(
        sf, settings.decay_half_life_days, settings.decay_threshold,
        settings.decay_protect_importance,
    )  # fmt: skip
    app.state.distill_service = DistillService(sf, settings.distill_threshold)
    app.state.review_service = ReviewService(sf)
    app.state.graph_export_service = GraphExportService(
        sf, settings.spine_weight_values(), settings.spine_freshness_half_life_days
    )
    idem = IdempotencyService(sf, settings.idempotency_ttl_hours)
    app.state.idempotency_service = idem
    app.state.index_worker_thread = worker_thread

    @app.middleware("http")
    async def request_id_mw(request: Request, call_next: Any) -> Any:
        rid = f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = rid
        is_mcp = request.headers.get("user-agent", "").startswith(MCP_USER_AGENT_PREFIX)
        if is_mcp:
            blocked = _mcp_block(request)
            if blocked is not None:
                log.info("request", request_id=rid, path=request.url.path, status=403,
                         client="mcp")  # fmt: skip
                return _error("MCP_DISABLED", blocked, rid, 403)
        key = request.headers.get("idempotency-key")
        if key is not None and request.method in _IDEMPOTENT_METHODS:
            response = await _idempotent(request, call_next, key, rid)
        else:
            # GET/HEAD are naturally repeat-safe: the header is accepted and ignored.
            response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        # ADR-028: origin is marked in the service log only (no audit column, no content).
        log.info("request", request_id=rid, path=request.url.path, status=response.status_code,
                 client="mcp" if is_mcp else "rest")  # fmt: skip
        return response

    def _mcp_block(request: Request) -> str | None:
        """Server-side backstop for the MCP switches. The key + ACL stay the real boundary."""
        if not settings.mcp_enabled:
            return "MCP is disabled (set AME_MCP_ENABLED=true and restart AME)"
        if (
            settings.mcp_read_only
            and request.method != "GET"
            and (request.url.path not in MCP_READ_POSTS)
        ):
            return "MCP is read-only (set AME_MCP_READ_ONLY=false and restart AME)"
        return None

    async def _idempotent(request: Request, call_next: Any, key: str, rid: str) -> Any:
        """ADR-027. Keys are scoped per authenticated caller; never logged."""
        try:
            actor = await run_in_threadpool(app.state.auth_service.authenticate, _bearer(request))
        except AMEError:
            return await call_next(request)  # the route itself answers 401
        body = await request.body()
        fp = request_fingerprint(request.method, request.url.path, request.url.query, body)
        try:
            replay = await run_in_threadpool(idem.begin, actor.actor_id, key, fp)
        except AMEError as exc:
            return _error(exc.code, exc.message, rid, exc.http_status, exc.details)
        if replay is not None:
            r = Response(replay.body, status_code=replay.status_code,
                         media_type="application/json")  # fmt: skip
            r.headers["Idempotency-Replayed"] = "true"
            return r
        try:
            response = await call_next(request)
            chunks = [c async for c in response.body_iterator]
        except BaseException:
            await run_in_threadpool(idem.abandon, actor.actor_id, key)
            raise
        raw = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        if response.status_code >= 500:
            await run_in_threadpool(idem.abandon, actor.actor_id, key)  # retry may run again
        else:
            await run_in_threadpool(
                idem.finish, actor.actor_id, key, response.status_code, raw.decode("utf-8")
            )
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(raw, status_code=response.status_code, headers=headers,
                        media_type=response.media_type)  # fmt: skip

    @app.exception_handler(AMEError)
    async def ame_error_handler(request: Request, exc: AMEError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "req_unknown")
        return _error(exc.code, exc.message, rid, exc.http_status, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "req_unknown")
        # Only locations + messages; never echo the submitted values (may contain content).
        errs = [
            {"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg", ""))}
            for e in exc.errors()
        ]
        return _error("VALIDATION_ERROR", "Request validation failed", rid, 422, {"errors": errs})

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        rid = getattr(request.state, "request_id", "req_unknown")
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _error(code, str(exc.detail), rid, exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "req_unknown")
        log.error("unhandled_error", request_id=rid, error=type(exc).__name__)
        return _error("INTERNAL_ERROR", "Internal server error", rid, 500)

    @app.get("/api/v1/health")
    def get_health() -> JSONResponse:
        result = health.get_health()
        return JSONResponse(status_code=200 if result["status"] == "ok" else 503, content=result)

    app.include_router(router)
    app.include_router(router_phase4)
    return app

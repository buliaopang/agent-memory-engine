"""REST v1 business routes (ADR-017). Thin adapter: parse -> call Application Service ->
serialize. No business rules and no SQL here (guarded by tests).

Phase 3a (ADR-024): the bearer token alone decides the actor - the local user token maps to
user:local, an agent key to that agent. The client cannot choose its identity (SECURITY #3).
Every read passes the actor so invisible memories/spaces answer 404 (ADR-025).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ame.application.auth_service import LOCAL_USER, AuthService
from ame.application.context_service import ContextService
from ame.application.graph_query_service import RELATIONS, GraphQueryService
from ame.application.index_worker import IndexWorker
from ame.application.memory_service import MemoryService
from ame.application.schemas import (
    Actor,
    AgentCreate,
    AgentCredential,
    AgentRead,
    AgentStateChange,
    AuditEventRead,
    ClassificationChange,
    ConflictRead,
    ConflictResolve,
    ContextPack,
    ContextRequest,
    EdgeCreate,
    EdgeRead,
    GraphView,
    LinkRevoke,
    MemoryCreate,
    MemoryPatch,
    MemoryRead,
    ReasonBody,
    RelationCreate,
    RelationRead,
    SearchRequest,
    SearchResponse,
    SpaceCreate,
    SpaceGrant,
    SpaceMemberRead,
    SpaceRead,
    StatsRead,
    StatusChange,
    VersionCreate,
    VersionRead,
)
from ame.application.search_service import SearchService
from ame.application.space_service import AgentService, SpaceService
from ame.errors import AuthenticationRequiredError
from ame.infrastructure.search_repository import STRUCTURAL

__all__ = ["LOCAL_USER", "router"]


def require_token(request: Request) -> Actor:
    auth = request.headers.get("authorization", "")
    scheme, _, presented = auth.partition(" ")
    if scheme.lower() != "bearer":
        raise AuthenticationRequiredError("Missing or invalid API token")
    a: AuthService = request.app.state.auth_service
    return a.authenticate(presented.strip())


def svc(request: Request) -> MemoryService:
    s: MemoryService = request.app.state.memory_service
    return s


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])
ActorDep = Depends(require_token)
SvcDep = Depends(svc)


@router.post("/memories", status_code=201)
def create_memory(body: MemoryCreate, s: MemoryService = SvcDep, a: Actor = ActorDep) -> MemoryRead:
    return s.create_memory(body, a)


@router.get("/memories/{memory_id}")
def get_memory(memory_id: str, s: MemoryService = SvcDep, a: Actor = ActorDep) -> MemoryRead:
    return s.get_memory(memory_id, a)


@router.patch("/memories/{memory_id}")
def patch_memory(
    memory_id: str, body: MemoryPatch, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> MemoryRead:
    return s.patch_memory(memory_id, body, a)


@router.post("/memories/{memory_id}/status")
def change_status(
    memory_id: str, body: StatusChange, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> MemoryRead:
    return s.change_status(memory_id, body, a)


@router.post("/memories/{memory_id}/classification")
def change_classification(
    memory_id: str, body: ClassificationChange, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> MemoryRead:
    return s.change_classification(memory_id, body, a)


@router.post("/memories/{memory_id}/archive")
def archive(
    memory_id: str, body: ReasonBody, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> MemoryRead:
    return s.archive_memory(memory_id, body.reason, a)


@router.post("/memories/{memory_id}/restore")
def restore(
    memory_id: str, body: ReasonBody, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> MemoryRead:
    return s.restore_memory(memory_id, body.reason, a)


@router.post("/memories/{memory_id}/versions", status_code=201)
def create_version(
    memory_id: str, body: VersionCreate, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> VersionRead:
    return s.create_version(memory_id, body, a)


@router.get("/memories/{memory_id}/versions")
def list_versions(
    memory_id: str, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> list[VersionRead]:
    return s.list_versions(memory_id, a)


@router.post("/edges")
def create_edge(
    body: EdgeCreate, response: Response, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> EdgeRead:
    r = s.create_edge(body, a)
    response.status_code = 201 if r.created else 200
    return r


@router.post("/edges/{edge_id}/revoke")
def revoke_edge(
    edge_id: str, body: LinkRevoke, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> EdgeRead:
    return s.revoke_edge(edge_id, body, a)


@router.get("/memories/{memory_id}/edges")
def list_edges(
    memory_id: str, include_revoked: bool = False, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> list[EdgeRead]:
    return s.list_edges(memory_id, include_revoked, a)


@router.post("/relations")
def create_relation(
    body: RelationCreate, response: Response, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> RelationRead:
    r = s.create_relation(body, a)
    response.status_code = 201 if r.created else 200
    return r


@router.post("/relations/{relation_id}/revoke")
def revoke_relation(
    relation_id: str, body: LinkRevoke, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> RelationRead:
    return s.revoke_relation(relation_id, body, a)


@router.get("/memories/{memory_id}/relations")
def list_relations(
    memory_id: str, include_revoked: bool = False, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> list[RelationRead]:
    return s.list_relations(memory_id, include_revoked, a)


@router.get("/memories/{memory_id}/audit")
def list_audit(
    memory_id: str, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> list[AuditEventRead]:
    return s.list_audit(memory_id, a)


@router.get("/memories/{memory_id}/conflicts")
def list_conflicts(
    memory_id: str, include_resolved: bool = False, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> list[ConflictRead]:
    return s.list_conflicts(memory_id, include_resolved, a)


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(
    conflict_id: str, body: ConflictResolve, s: MemoryService = SvcDep, a: Actor = ActorDep
) -> ConflictRead:
    return s.resolve_conflict(conflict_id, body, a)


# ---------- Phase 2: search / graph / stats (ADR-020, P2-8) ----------
@router.post("/memories/search")
def search_memories(body: SearchRequest, request: Request, a: Actor = ActorDep) -> SearchResponse:
    s: SearchService = request.app.state.search_service
    return s.search(body, actor=a)


@router.post("/context")
def build_context(body: ContextRequest, request: Request, a: Actor = ActorDep) -> ContextPack:
    """Phase 3c (ADR-028): read-only BuildContext so MCP memory_get_context stays a REST client
    (D-Q4). Same ACL as search; memory text is labelled as DATA (SECURITY #9)."""
    c: ContextService = request.app.state.context_service
    return c.build(body, actor=a)


@router.get("/memories/{memory_id}/neighbors")
def neighbors(
    memory_id: str,
    request: Request,
    direction: str = "both",
    depth: int = 1,
    limit: int = 50,
    edge_types: str | None = None,
    relation_types: str | None = None,
    a: Actor = ActorDep,
) -> GraphView:
    g: GraphQueryService = request.app.state.graph_query_service
    et = tuple(x for x in edge_types.split(",") if x) if edge_types is not None else STRUCTURAL
    rt = (
        tuple(x for x in relation_types.split(",") if x)
        if relation_types is not None
        else RELATIONS
    )
    return g.neighbors(memory_id, et, rt, direction, depth, limit, actor=a)


@router.get("/graph")
def graph(
    request: Request,
    space_id: str | None = None,
    center_id: str | None = None,
    depth: int = 1,
    limit: int = 200,
    a: Actor = ActorDep,
) -> GraphView:
    g: GraphQueryService = request.app.state.graph_query_service
    return g.graph(space_id, center_id, depth, limit, actor=a)


@router.get("/stats")
def stats(request: Request, a: Actor = ActorDep) -> StatsRead:
    s: SearchService = request.app.state.search_service
    w: IndexWorker = request.app.state.index_worker
    st = w.status()
    return StatsRead(
        memories=s.stats(a),
        outbox=st["outbox"],
        vector_state=st["vector_state"],
        retrieval_mode=st["mode"],
    )


# ---------- Phase 3a: spaces / agents registry (ADR-024/025). Writes are user-only. ----------
def _spaces(request: Request) -> SpaceService:
    s: SpaceService = request.app.state.space_service
    return s


def _agents(request: Request) -> AgentService:
    s: AgentService = request.app.state.agent_service
    return s


SpacesDep = Depends(_spaces)
AgentsDep = Depends(_agents)


@router.get("/spaces")
def list_spaces(s: SpaceService = SpacesDep, a: Actor = ActorDep) -> list[SpaceRead]:
    return s.list_spaces(a)


@router.post("/spaces", status_code=201)
def create_space(body: SpaceCreate, s: SpaceService = SpacesDep, a: Actor = ActorDep) -> SpaceRead:
    return s.create_space(body, a)


@router.get("/spaces/{space_id}")
def get_space(space_id: str, s: SpaceService = SpacesDep, a: Actor = ActorDep) -> SpaceRead:
    return s.get_space(space_id, a)


@router.get("/spaces/{space_id}/members")
def list_members(
    space_id: str, s: SpaceService = SpacesDep, a: Actor = ActorDep
) -> list[SpaceMemberRead]:
    return s.list_members(space_id, a)


@router.post("/spaces/{space_id}/members")
def grant_member(
    space_id: str, body: SpaceGrant, s: SpaceService = SpacesDep, a: Actor = ActorDep
) -> SpaceMemberRead:
    return s.grant(space_id, body, a)


@router.post("/spaces/{space_id}/members/{agent_id}/revoke")
def revoke_member(
    space_id: str,
    agent_id: str,
    body: ReasonBody,
    s: SpaceService = SpacesDep,
    a: Actor = ActorDep,
) -> SpaceMemberRead:
    return s.revoke(space_id, agent_id, body.reason, a)


@router.get("/agents")
def list_agents(s: AgentService = AgentsDep, a: Actor = ActorDep) -> list[AgentRead]:
    return s.list_agents(a)


@router.post("/agents", status_code=201)
def register_agent(
    body: AgentCreate, s: AgentService = AgentsDep, a: Actor = ActorDep
) -> AgentRead:
    return s.register_agent(body, a)


@router.post("/agents/{agent_id}/credentials", status_code=201)
def issue_credential(
    agent_id: str, body: AgentStateChange, s: AgentService = AgentsDep, a: Actor = ActorDep
) -> AgentCredential:
    """Returns the new key ONCE; any previous key of this agent stops working."""
    return s.issue_credential(agent_id, a, body.reason)


@router.post("/agents/{agent_id}/disable")
def disable_agent(
    agent_id: str, body: AgentStateChange, s: AgentService = AgentsDep, a: Actor = ActorDep
) -> AgentRead:
    return s.set_enabled(agent_id, False, a, body.reason)


@router.post("/agents/{agent_id}/enable")
def enable_agent(
    agent_id: str, body: AgentStateChange, s: AgentService = AgentsDep, a: Actor = ActorDep
) -> AgentRead:
    return s.set_enabled(agent_id, True, a, body.reason)

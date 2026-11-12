"""The 18 MCP tools (ADR-028, Phase 4 ADR-029/030/031) as plain Python: each maps 1:1 onto an
existing REST route, so the 3a permissions (404 invisible / 403 denied / agent writes =
candidate) and 3b idempotency are enforced by the server, never here. No ``mcp`` import here.

Phase 4 (L6): ``memory_feedback`` is implemented (write tool: not registered in read-only mode)
plus two read tools, ``memory_spine`` and ``memory_list_epochs``. No admin tools
(spaces/agents/grants/conflict resolution/epoch changes/distill/promote) are exposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ame.adapters.mcp.client import ApiResult, RestClient, seg
from ame.adapters.mcp.render import error_result, ok_result


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    read_only: bool
    destructive: bool = False


READ_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("memory_search", "Search memories you are allowed to see (hybrid/lexical).", True),
    ToolSpec("memory_read", "Read one memory by id (optionally its edges/relations).", True),
    ToolSpec("memory_get_context", "Pack relevant memories for a task into a token budget.", True),
    ToolSpec("memory_neighbors", "Graph neighbours of a memory.", True),
    ToolSpec("memory_list_spaces", "List the memory spaces visible to you.", True),
    ToolSpec("memory_get_stats", "Counts by status and index state (no content).", True),
    ToolSpec(
        "memory_spine", "The most load-bearing active memories, each with its score reasons.", True
    ),  # fmt: skip
    ToolSpec("memory_list_epochs", "List the user's epochs (periods) visible to you.", True),
)
WRITE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "memory_write",
        "Proactively write a new memory to persist facts, decisions, or progress (auto-approved).",
        False,
    ),  # fmt: skip
    ToolSpec("memory_create_version", "Add a new version (needs expected_version).", False),
    ToolSpec(
        "memory_link", "Create a structural edge (PARENT/REFERENCE/SUPERSEDES/DERIVED_FROM).", False
    ),  # fmt: skip
    ToolSpec(
        "memory_relate", "Create a semantic relation (CONTRADICTS/SUPPORTS/RELATED_TO).", False
    ),  # fmt: skip
    ToolSpec("memory_revoke_edge", "Revoke a structural edge (reason required).", False, True),
    ToolSpec(
        "memory_revoke_relation", "Revoke a semantic relation (reason required).", False, True
    ),  # fmt: skip
    ToolSpec(
        "memory_change_status", "Change status per the state machine (reason required).", False
    ),  # fmt: skip
    ToolSpec("memory_change_classification", "Change layer / memory_type.", False),
    ToolSpec(
        "memory_archive", "Archive a memory (reason required; reversible by the user).", False, True
    ),  # fmt: skip
    ToolSpec(
        "memory_feedback",
        "Give feedback on a memory you can see: useful / not_useful / outdated / wrong.",
        False,
    ),  # fmt: skip
)
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS
DEFERRED_TOOLS: tuple[str, ...] = ()  # Phase 4: memory_feedback implemented


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class AmeTools:
    def __init__(self, client: RestClient, max_content_chars: int = 4000) -> None:
        self._c = client
        self._max = max_content_chars

    def _out(self, r: ApiResult) -> dict[str, Any]:
        if r.ok:
            return ok_result(r.data, self._max, r.replayed)
        return error_result(r.error_code or "ERROR", r.error_message, r.status, r.request_id)

    # ---------------- read ----------------
    def memory_search(
        self,
        query: str,
        scope_ids: list[str] | None = None,
        layers: list[str] | None = None,
        memory_types: list[str] | None = None,
        top_k: int = 10,
        include_graph_context: bool = False,
        include_sources: bool = True,
        include_conflicts: bool = False,
    ) -> dict[str, Any]:
        body = {"query": query, "scope_ids": scope_ids or [], "layers": layers or [],
                "memory_types": memory_types or [], "top_k": top_k,
                "include_graph_context": include_graph_context,
                "include_sources": include_sources,
                "include_conflicts": include_conflicts}  # fmt: skip
        return self._out(self._c.post_read("/memories/search", body))

    def memory_read(
        self,
        memory_id: str,
        include_sources: bool = True,
        include_edges: bool = False,
        include_relations: bool = False,
    ) -> dict[str, Any]:
        r = self._c.get(f"/memories/{seg(memory_id)}")
        if not r.ok:
            return self._out(r)
        data: dict[str, Any] = {"memory": dict(r.data)}
        if not include_sources:
            data["memory"].pop("sources", None)
        if include_edges:
            e = self._c.get(f"/memories/{seg(memory_id)}/edges")
            if not e.ok:
                return self._out(e)
            data["edges"] = e.data
        if include_relations:
            rel = self._c.get(f"/memories/{seg(memory_id)}/relations")
            if not rel.ok:
                return self._out(rel)
            data["relations"] = rel.data
        return ok_result(data, self._max)

    def memory_get_context(
        self,
        task_description: str,
        scope_ids: list[str] | None = None,
        layers: list[str] | None = None,
        memory_types: list[str] | None = None,
        token_budget: int = 2000,
        include_procedural: bool = True,
        include_sources: bool = True,
    ) -> dict[str, Any]:
        body = {"task_description": task_description, "scope_ids": scope_ids or [],
                "layers": layers or [], "memory_types": memory_types or [],
                "token_budget": token_budget, "include_procedural": include_procedural,
                "include_sources": include_sources}  # fmt: skip
        return self._out(self._c.post_read("/context", body))

    def memory_neighbors(
        self,
        memory_id: str,
        edge_types: list[str] | None = None,
        relation_types: list[str] | None = None,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        include_revoked: bool = False,
    ) -> dict[str, Any]:
        q: dict[str, Any] = {"direction": direction, "depth": depth, "limit": limit,
                             "edge_types": ",".join(edge_types) if edge_types is not None else None,
                             "relation_types": ",".join(relation_types)
                             if relation_types is not None else None}  # fmt: skip
        # include_revoked: neighbours are computed over active links only (REST has no flag);
        # accepted for contract compatibility and reported back.
        out = self._out(self._c.get(f"/memories/{seg(memory_id)}/neighbors", q))
        if include_revoked and out["ok"]:
            out["note"] = "include_revoked is not supported; only active links are returned"
        return out

    def memory_list_spaces(self) -> dict[str, Any]:
        return self._out(self._c.get("/spaces"))

    def memory_get_stats(self) -> dict[str, Any]:
        return self._out(self._c.get("/stats"))

    # ---------------- write (auto Idempotency-Key) ----------------
    def memory_write(
        self,
        content: str,
        memory_type: str,
        layer: str,
        scope_type: str,
        scope_id: str,
        importance: float,
        confidence: float,
        summary: str | None = None,
        tags: list[str] | None = None,
        parent_ids: list[str] | None = None,
        reference_ids: list[str] | None = None,
        sources: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({"content": content, "summary": summary, "memory_type": memory_type,
                           "layer": layer, "scope_type": scope_type, "scope_id": scope_id,
                           "importance": importance, "confidence": confidence,
                           "tags": tags or [], "parent_ids": parent_ids or [],
                           "reference_ids": reference_ids or [], "sources": sources or [],
                           "metadata": metadata or {}})  # fmt: skip
        return self._out(self._c.write("/memories", body, idempotency_key))

    def memory_create_version(
        self,
        memory_id: str,
        content: str,
        change_reason: str,
        expected_version: int,
        summary: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({"content": content, "summary": summary, "change_reason": change_reason,
                           "expected_version": expected_version})  # fmt: skip
        return self._out(self._c.write(f"/memories/{seg(memory_id)}/versions", body,
                                       idempotency_key))  # fmt: skip

    def memory_link(
        self,
        from_memory_id: str,
        to_memory_id: str,
        edge_type: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = {"from_memory_id": from_memory_id, "to_memory_id": to_memory_id,
                "edge_type": edge_type, "metadata": metadata or {}}  # fmt: skip
        return self._out(self._c.write("/edges", body, idempotency_key))

    def memory_relate(
        self,
        from_memory_id: str,
        to_memory_id: str,
        relation_type: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = {"from_memory_id": from_memory_id, "to_memory_id": to_memory_id,
                "relation_type": relation_type, "metadata": metadata or {}}  # fmt: skip
        return self._out(self._c.write("/relations", body, idempotency_key))

    def memory_revoke_edge(
        self, edge_id: str, reason: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._out(self._c.write(f"/edges/{seg(edge_id)}/revoke", {"reason": reason},
                                       idempotency_key))  # fmt: skip

    def memory_revoke_relation(
        self, relation_id: str, reason: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._out(self._c.write(f"/relations/{seg(relation_id)}/revoke",
                                       {"reason": reason}, idempotency_key))  # fmt: skip

    def memory_change_status(
        self,
        memory_id: str,
        to_status: str,
        reason: str,
        superseded_by_memory_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({"to_status": to_status, "reason": reason,
                           "superseded_by_memory_id": superseded_by_memory_id})  # fmt: skip
        return self._out(self._c.write(f"/memories/{seg(memory_id)}/status", body,
                                       idempotency_key))  # fmt: skip

    def memory_change_classification(
        self,
        memory_id: str,
        layer: str | None = None,
        memory_type: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({"layer": layer, "memory_type": memory_type, "reason": reason})
        return self._out(self._c.write(f"/memories/{seg(memory_id)}/classification", body,
                                       idempotency_key))  # fmt: skip

    def memory_archive(
        self, memory_id: str, reason: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._out(self._c.write(f"/memories/{seg(memory_id)}/archive", {"reason": reason},
                                       idempotency_key))  # fmt: skip

    # ---------------- Phase 4 ----------------
    def memory_spine(
        self, scope_id: str | None = None, epoch_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        q = _drop_none({"scope_id": scope_id, "epoch_id": epoch_id, "limit": limit})
        return self._out(self._c.get("/spine", q))

    def memory_list_epochs(self) -> dict[str, Any]:
        return self._out(self._c.get("/epochs"))

    def memory_feedback(
        self, memory_id: str, feedback: str, reason: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._out(self._c.write(f"/memories/{seg(memory_id)}/feedback",
                                       {"feedback": feedback, "reason": reason},
                                       idempotency_key))  # fmt: skip

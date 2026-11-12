"""Phase 5 tests: Conflux Tree-Graph consensus & Package B feature ports (ADR-033, ADR-034).

Covers:
1. Single ``PARENT`` constraint (at most 1 active incoming ``PARENT`` edge per memory) across
   Pydantic validation, ``GraphService``, SQLite trigger ``trg_memory_edges_single_parent``,
   re-parenting after revocation, and ``ame check-graph`` (G008 / G017).
2. Heaviest-subtree Pivot Chain (GHOST / Conflux Spine selection) using 5-factor Spine weights.
3. Auto-derived Conflux epochs ``Past(p_i) \\ union_{j < i} Past(p_j)`` and deterministic
   topological total order (including ``REFERENCE`` reachability and unanchored tail).
4. Conflict resolution "first-arrived in total order wins" suggestion (human-confirmed).
5. Static self-contained HTML/JSON ``export-graph`` (CLI & REST, ACL-aware, zero CDN) and
   optional local ``FastEmbedProvider`` with graceful fallback to ``lexical_graph``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from ame.application.integrity_service import IntegrityService
from ame.application.schemas import (
    ConflictResolution,
    EdgeCreate,
    LinkRevoke,
    MemoryCreate,
    SearchRequest,
    SourceCreate,
)
from ame.cli import main as cli_main
from ame.config import Settings
from ame.domain.conflux import (
    compute_conflux_consensus,
    suggest_conflict_winner,
)
from ame.domain.enums import EdgeType, Layer, MemoryType, ScopeType, SourceType
from ame.errors import ParentAlreadyExistsError
from ame.infrastructure.database import create_sqlite_engine, run_migrations
from ame.infrastructure.embeddings import FastEmbedProvider, build_vector_backend
from ame.infrastructure.unit_of_work import make_session_factory
from tests.conftest import USER


def _mk(
    content: str = "fact",
    *,
    scope_id: str = "shared",
    importance: float = 0.5,
    n_sources: int = 1,
    parent_ids: list[str] | None = None,
    reference_ids: list[str] | None = None,
) -> MemoryCreate:
    return MemoryCreate(
        content=content,
        summary=content[:40],
        memory_type=MemoryType.SEMANTIC,
        layer=Layer.L2,
        scope_type=ScopeType.SHARED,
        scope_id=scope_id,
        importance=importance,
        confidence=0.8,
        sources=[
            SourceCreate(source_type=SourceType.MANUAL, source_ref=f"src:test:{i}")
            for i in range(n_sources)
        ],
        parent_ids=parent_ids or [],
        reference_ids=reference_ids or [],
    )


# ---------------------------------------------------------------------------
# 1. Single PARENT constraint (parent tree)
# ---------------------------------------------------------------------------


def test_memory_create_rejects_multiple_distinct_parents() -> None:
    with pytest.raises(ValidationError, match="at most one parent_id"):
        _mk("child", parent_ids=["mem_p1", "mem_p2"])

    # Duplicate of the same parent_id is deduplicated cleanly; multiple references are allowed.
    cmd = _mk("child", parent_ids=["mem_p1", "mem_p1"], reference_ids=["mem_r1", "mem_r2"])
    assert cmd.parent_ids == ["mem_p1"]
    assert cmd.reference_ids == ["mem_r1", "mem_r2"]


def test_single_parent_enforced_in_service_trigger_and_reparenting(
    engine, svc, tmp_path: Path
) -> None:
    p1 = svc.create_memory(_mk("parent 1"), USER)
    p2 = svc.create_memory(_mk("parent 2"), USER)
    child = svc.create_memory(_mk("child", parent_ids=[p1.memory_id]), USER)

    edges = svc.list_edges(child.memory_id, actor=USER)
    parent_edges = [
        e for e in edges if e.to_memory_id == child.memory_id and e.edge_type is EdgeType.PARENT
    ]
    assert len(parent_edges) == 1
    first_edge_id = parent_edges[0].edge_id

    # Idempotent repeat of the exact same PARENT edge returns created=False.
    same = svc.create_edge(
        EdgeCreate(
            from_memory_id=p1.memory_id,
            to_memory_id=child.memory_id,
            edge_type=EdgeType.PARENT,
        ),
        USER,
    )
    assert same.created is False and same.edge_id == first_edge_id

    # Second distinct PARENT edge to the same child is rejected with ParentAlreadyExistsError.
    with pytest.raises(ParentAlreadyExistsError) as exc_info:
        svc.create_edge(
            EdgeCreate(
                from_memory_id=p2.memory_id,
                to_memory_id=child.memory_id,
                edge_type=EdgeType.PARENT,
            ),
            USER,
        )
    assert exc_info.value.code == "PARENT_ALREADY_EXISTS"
    assert exc_info.value.details["existing_parent_id"] == p1.memory_id

    # Extra predecessor via REFERENCE edge is allowed.
    ref = svc.create_edge(
        EdgeCreate(
            from_memory_id=p2.memory_id,
            to_memory_id=child.memory_id,
            edge_type=EdgeType.REFERENCE,
        ),
        USER,
    )
    assert ref.created is True and ref.edge_type is EdgeType.REFERENCE

    # Re-parenting: revoking the old PARENT edge allows attaching a new PARENT edge.
    svc.revoke_edge(first_edge_id, LinkRevoke(reason="re-parent to p2"), USER)
    new_parent = svc.create_edge(
        EdgeCreate(
            from_memory_id=p2.memory_id,
            to_memory_id=child.memory_id,
            edge_type=EdgeType.PARENT,
        ),
        USER,
    )
    assert new_parent.created is True and new_parent.from_memory_id == p2.memory_id

    # Raw SQLite trigger check: bypassing GraphService fails on trg_memory_edges_single_parent.
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        with pytest.raises(sqlite3.IntegrityError, match="PARENT_ALREADY_EXISTS"):
            cur.execute(
                "INSERT INTO memory_edges (edge_id, from_memory_id, to_memory_id, edge_type, "
                "status, created_at, created_by) VALUES "
                "('edg_raw_dup', ?, ?, 'PARENT', 'active', '2026-01-01T00:00:00Z', 'user:local')",
                (p1.memory_id, child.memory_id),
            )
    finally:
        raw.close()


def test_check_graph_verifies_single_parent_trigger_and_g017(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path)
    run_migrations(s)
    rep = IntegrityService(s.db_path).run()
    assert rep.exit_code == 0
    assert "G017" in rep.checked


# ---------------------------------------------------------------------------
# 2. Heaviest-subtree Pivot Chain & 3. Auto-derived Epochs + Total Order
# ---------------------------------------------------------------------------


def test_conflux_heaviest_subtree_beats_longer_lighter_chain() -> None:
    """Conflux paper §3.2 / Fig. 2: a shorter branch with larger total subtree weight beats a
    longer branch with smaller total weight, and epochs/total-order follow Past(p_i)."""
    # Parent tree:
    #   genesis -> a1 -> a2 -> a3   (weights: a1=0.2, a2=0.2, a3=0.2 => subW(a1)=0.6)
    #   genesis -> b1 -> b2         (weights: b1=0.5, b2=0.4        => subW(b1)=0.9)
    # Plus a REFERENCE edge from a2 -> b2 (so b2 references side-chain block a2, pulling a1 & a2
    # into Epoch(b2), while a3 is not referenced by the pivot chain and lands in unanchored tail).
    nodes = ["genesis", "a1", "a2", "a3", "b1", "b2"]
    weights = {
        "genesis": 0.5,
        "a1": 0.2,
        "a2": 0.2,
        "a3": 0.2,
        "b1": 0.5,
        "b2": 0.4,
    }
    edges = [
        ("genesis", "a1", "PARENT"),
        ("a1", "a2", "PARENT"),
        ("a2", "a3", "PARENT"),
        ("genesis", "b1", "PARENT"),
        ("b1", "b2", "PARENT"),
        ("a2", "b2", "REFERENCE"),
    ]
    c = compute_conflux_consensus(nodes, weights, edges)

    # Subtree weights: genesis = 0.5 + 0.6 + 0.9 = 2.0; b1 = 0.9 > a1 = 0.6
    assert c.subtree_weights["genesis"] == pytest.approx(2.0)
    assert c.subtree_weights["b1"] == pytest.approx(0.9)
    assert c.subtree_weights["a1"] == pytest.approx(0.6)

    # Pivot chain follows heaviest subtree: genesis -> b1 -> b2
    assert c.pivot_chain == ("genesis", "b1", "b2")

    # Derived epochs:
    # Epoch 0 (pivot=genesis): {genesis}
    # Epoch 1 (pivot=b1):      {b1}
    # Epoch 2 (pivot=b2):      {a1, a2, b2} (topologically ordered: a1 -> a2 -> b2)
    # Epoch 3 (pivot=None):    {a3} (unanchored tail)
    assert len(c.epochs) == 4
    assert c.epochs[0].pivot_memory_id == "genesis" and c.epochs[0].memory_ids == ("genesis",)
    assert c.epochs[1].pivot_memory_id == "b1" and c.epochs[1].memory_ids == ("b1",)
    assert c.epochs[2].pivot_memory_id == "b2" and c.epochs[2].memory_ids == ("a1", "a2", "b2")
    assert c.epochs[3].pivot_memory_id is None and c.epochs[3].memory_ids == ("a3",)

    assert c.total_order == ("genesis", "b1", "a1", "a2", "b2", "a3")

    # Conflict suggestion between b1 (rank 1, epoch 1) and a1 (rank 2, epoch 2):
    sug = suggest_conflict_winner("a1", "b1", c)
    assert sug is not None
    assert sug.winner_memory_id == "b1"
    assert sug.loser_memory_id == "a1"
    assert sug.recommended_resolution == "a_superseded_by_b"
    assert sug.winner_epoch == 1 and sug.loser_epoch == 2


# ---------------------------------------------------------------------------
# 4. End-to-End REST & CLI: Spine, Derived Epochs, Conflict Suggestion, Graph Export
# ---------------------------------------------------------------------------


def test_rest_and_cli_conflux_spine_conflict_suggestion_and_export(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from ame.adapters.rest.app import create_app
    from ame.infrastructure.token_store import default_protector, load_or_create_token

    settings = Settings(data_dir=tmp_path, index_worker_enabled=False)
    app = create_app(settings)
    token = load_or_create_token(settings.data_dir, default_protector())
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        # Create genesis and two competing branches on the PARENT tree:
        # genesis -> light1 -> light2 (low importance = 0.1)
        # genesis -> heavy1 (high importance = 1.0)
        r_gen = client.post(
            "/api/v1/memories",
            headers=headers,
            json=_mk("genesis root memory", importance=0.8).model_dump(mode="json"),
        )
        assert r_gen.status_code == 201
        gen_id = r_gen.json()["memory_id"]

        r_l1 = client.post(
            "/api/v1/memories",
            headers=headers,
            json=_mk(
                "light branch step 1", importance=0.0, n_sources=0, parent_ids=[gen_id]
            ).model_dump(mode="json"),
        )
        l1_id = r_l1.json()["memory_id"]

        r_l2 = client.post(
            "/api/v1/memories",
            headers=headers,
            json=_mk(
                "light branch step 2", importance=0.0, n_sources=0, parent_ids=[l1_id]
            ).model_dump(mode="json"),
        )
        l2_id = r_l2.json()["memory_id"]

        r_h1 = client.post(
            "/api/v1/memories",
            headers=headers,
            json=_mk(
                "heavy branch main memory",
                importance=1.0,
                n_sources=3,
                parent_ids=[gen_id],
            ).model_dump(mode="json"),
        )
        h1_id = r_h1.json()["memory_id"]
        # Reference side-chain memory l1_id -> h1_id (l1 happened before h1, ADR-019/033)
        r_ref = client.post(
            "/api/v1/edges",
            headers=headers,
            json={
                "from_memory_id": l1_id,
                "to_memory_id": h1_id,
                "edge_type": "REFERENCE",
            },
        )
        assert r_ref.status_code == 201
        # Give heavy1 positive user feedback so its subtree weight clearly exceeds light1+light2.
        for actor_idx in range(3):
            app.state.feedback_service.add(
                h1_id,
                __import__("ame.application.schemas", fromlist=["FeedbackCreate"]).FeedbackCreate(
                    feedback="useful", reason="core main memory"
                ),
                __import__("ame.application.schemas", fromlist=["Actor"]).Actor(
                    actor_type="user", actor_id=f"user:voter{actor_idx}"
                ),
            )
        for actor_idx in range(3):
            app.state.feedback_service.add(
                l1_id,
                __import__("ame.application.schemas", fromlist=["FeedbackCreate"]).FeedbackCreate(
                    feedback="wrong", reason="low quality branch"
                ),
                __import__("ame.application.schemas", fromlist=["Actor"]).Actor(
                    actor_type="user", actor_id=f"user:voter{actor_idx}"
                ),
            )
            app.state.feedback_service.add(
                l2_id,
                __import__("ame.application.schemas", fromlist=["FeedbackCreate"]).FeedbackCreate(
                    feedback="wrong", reason="low quality branch"
                ),
                __import__("ame.application.schemas", fromlist=["Actor"]).Actor(
                    actor_type="user", actor_id=f"user:voter{actor_idx}"
                ),
            )

        # Query GET /api/v1/spine
        sp = client.get("/api/v1/spine", headers=headers).json()
        assert sp["pivot_chain"] == [gen_id, h1_id]
        # Epoch 0 = [gen_id], Epoch 1 (pivot=h1_id) = [l1_id, h1_id] (since h1 references l1),
        # Epoch 2 (unanchored) = [l2_id]
        assert sp["total_order"] == [gen_id, l1_id, h1_id, l2_id]

        # Query GET /api/v1/epochs/derived
        eps = client.get("/api/v1/epochs/derived", headers=headers).json()
        assert len(eps) == 3
        assert eps[0]["pivot_memory_id"] == gen_id and eps[0]["memory_ids"] == [gen_id]
        assert eps[1]["pivot_memory_id"] == h1_id and eps[1]["memory_ids"] == [l1_id, h1_id]
        assert eps[2]["pivot_memory_id"] is None and eps[2]["memory_ids"] == [l2_id]

        # Declare a CONTRADICTS relation between l2_id (rank 3) and h1_id (rank 2)
        rel_res = client.post(
            "/api/v1/relations",
            headers=headers,
            json={
                "from_memory_id": l2_id,
                "to_memory_id": h1_id,
                "relation_type": "CONTRADICTS",
            },
        )
        assert rel_res.status_code == 201

        cfls = client.get(f"/api/v1/memories/{h1_id}/conflicts", headers=headers).json()
        assert len(cfls) == 1
        cfl = cfls[0]
        sug = cfl["suggestion"]
        assert sug is not None
        assert sug["winner_memory_id"] == h1_id
        assert sug["loser_memory_id"] == l2_id
        expected_res = (
            ConflictResolution.B_SUPERSEDED_BY_A.value
            if cfl["memory_a_id"] == h1_id
            else ConflictResolution.A_SUPERSEDED_BY_B.value
        )
        assert sug["recommended_resolution"] == expected_res
        assert sug["winner_rank"] < sug["loser_rank"]

        # Human confirms the suggested resolution:
        resolved = client.post(
            f"/api/v1/conflicts/{cfl['conflict_id']}/resolve",
            headers=headers,
            json={
                "resolution": sug["recommended_resolution"],
                "reason": "accept Conflux first-arrived winner",
                "evidence": {"checked_by": "operator"},
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"
        assert resolved.json()["suggestion"]["winner_memory_id"] == h1_id

        # Export graph via REST (JSON and HTML)
        exp_json = client.get("/api/v1/graph/export?format=json", headers=headers)
        assert exp_json.status_code == 200
        gdata = exp_json.json()
        assert gdata["pivot_chain"] == [gen_id, h1_id]
        assert len(gdata["nodes"]) == 4

        exp_html = client.get("/api/v1/graph/export?format=html", headers=headers)
        assert exp_html.status_code == 200
        assert "<!DOCTYPE html>" in exp_html.text
        assert "http://" not in exp_html.text and "https://" not in exp_html.text

    # Test CLI commands: `ame spine`, `ame epoch derive`, `ame export-graph`
    out_html = tmp_path / "graph.html"
    out_json = tmp_path / "graph.json"
    assert cli_main(["spine", "--db", str(settings.db_path), "--json"]) == 0
    assert cli_main(["epoch", "--db", str(settings.db_path), "derive", "--json"]) == 0
    assert (
        cli_main(
            [
                "export-graph",
                "--db",
                str(settings.db_path),
                "--out",
                str(out_html),
                "--format",
                "html",
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "export-graph",
                "--db",
                str(settings.db_path),
                "--out",
                str(out_json),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert out_html.exists() and "<svg" in out_html.read_text(encoding="utf-8")
    loaded = json.loads(out_json.read_text(encoding="utf-8"))
    assert loaded["pivot_chain"] == [gen_id, h1_id]


# ---------------------------------------------------------------------------
# 5. Optional Local FastEmbedProvider & Hybrid Vector Fallback
# ---------------------------------------------------------------------------


def test_fastembed_provider_fallback_and_mocked_hybrid_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 1. Settings accepts "fastembed" and "none", rejects cloud providers
    s_fe = Settings(
        data_dir=tmp_path,
        embedding_provider="fastembed",
        vector_store_backend="memory",
    )
    assert s_fe.embedding_provider == "fastembed"

    # 2. When fastembed is not installed, build_vector_backend degrades cleanly to None
    import builtins

    real_import = builtins.__import__

    def _no_fastembed(name: str, *args: object, **kwargs: object) -> object:
        if name == "fastembed":
            raise ImportError("mocked missing fastembed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fastembed)
    assert build_vector_backend(s_fe) is None
    monkeypatch.setattr(builtins, "__import__", real_import)

    # 3. With a mocked local fastembed module, FastEmbedProvider + InMemoryVectorStore run hybrid
    fake_fe = types.ModuleType("fastembed")

    class _FakeTextEmbedding:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        def embed(self, texts: list[str]) -> list[list[float]]:
            out: list[list[float]] = []
            for t in texts:
                if "conflux" in t.lower() or "pivot" in t.lower():
                    out.append([1.0, 0.0, 0.0, 0.0])
                else:
                    out.append([0.0, 1.0, 0.0, 0.0])
            return out

    fake_fe.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake_fe)

    provider = FastEmbedProvider(model_name="BAAI/bge-small-zh-v1.5")
    assert provider.model_name == "BAAI/bge-small-zh-v1.5"
    assert provider.dimension == 4
    vecs = provider.embed(["Conflux pivot chain"])
    assert vecs == [[1.0, 0.0, 0.0, 0.0]]

    backend = build_vector_backend(s_fe)
    assert backend is not None
    assert backend.provider.dimension == 4

    run_migrations(s_fe)
    eng = create_sqlite_engine(s_fe)
    try:
        sf = make_session_factory(eng)
        from ame.application.index_worker import IndexWorker
        from ame.application.memory_service import MemoryService
        from ame.application.search_service import SearchService

        msvc = MemoryService(sf)
        m = msvc.create_memory(_mk("Conflux pivot chain consensus memory"), USER)
        worker = IndexWorker(sf, backend=backend)
        stats = worker.run_until_idle()
        assert stats.done >= 1 and worker.mode == "hybrid"

        ssvc = SearchService(sf, vector=backend)
        res = ssvc.search(SearchRequest(query="conflux pivot"), actor=USER)
        assert res.retrieval_mode == "hybrid"
        assert any(r.memory.memory_id == m.memory_id for r in res.results)
    finally:
        eng.dispose()

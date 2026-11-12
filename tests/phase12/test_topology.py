"""Step 4: topological order (pure + read-only service + CLI), ADR-019."""

from __future__ import annotations

import hashlib
import json

import pytest

from ame.application.schemas import EdgeCreate, LinkRevoke, RelationCreate
from ame.application.topology_service import TopologyService
from ame.cli import main
from ame.domain.enums import EdgeType
from ame.domain.topology import topological_order
from ame.errors import CycleDetectedError, ValidationFailedError
from tests.conftest import USER, make


def test_linear_and_isolated() -> None:
    assert topological_order(["c", "b", "a", "z"], [("a", "b"), ("b", "c")]) == ["a", "b", "c", "z"]


def test_diamond_stable() -> None:
    e = [("a", "c"), ("a", "b"), ("b", "d"), ("c", "d")]
    assert topological_order("abcd", e) == ["a", "b", "c", "d"]


def test_multi_root_and_duplicate_edges() -> None:
    e = [("y", "z"), ("x", "z"), ("x", "z")]
    assert topological_order(["z", "y", "x"], e) == ["x", "y", "z"]


def test_cycle_raises() -> None:
    with pytest.raises(CycleDetectedError):
        topological_order("ab", [("a", "b"), ("b", "a")])
    with pytest.raises(CycleDetectedError):
        topological_order("a", [("a", "a")])


def _sha(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_service_orders_active_structural_edges_only(svc, settings, engine) -> None:
    a = svc.create_memory(make(), USER).memory_id
    b = svc.create_memory(make(parent_ids=[a]), USER).memory_id  # a -> b
    c = svc.create_memory(make(), USER).memory_id
    e = svc.create_edge(
        EdgeCreate(from_memory_id=c, to_memory_id=a, edge_type=EdgeType.REFERENCE), USER
    )  # c -> a
    # semantic relation b SUPPORTS c must NOT affect order
    svc.create_relation(RelationCreate(from_memory_id=b, to_memory_id=c, relation_type="SUPPORTS"),
                        USER)  # fmt: skip
    engine.dispose()
    res = TopologyService(settings.db_path).order()
    assert res.memory_ids == [c, a, b] and res.edge_count == 2 and not res.truncated
    only_parent = TopologyService(settings.db_path).order([EdgeType.PARENT])
    assert only_parent.edge_count == 1
    svc.revoke_edge(e.edge_id, LinkRevoke(reason="test"), USER)
    engine.dispose()
    res2 = TopologyService(settings.db_path).order()
    assert res2.edge_count == 1
    assert res2.memory_ids.index(a) < res2.memory_ids.index(b)


def test_service_is_read_only_and_limits(svc, settings, engine) -> None:
    for _ in range(5):
        svc.create_memory(make(), USER)
    engine.dispose()
    h = _sha(settings.db_path)
    res = TopologyService(settings.db_path).order(limit=2)
    assert res.truncated and len(res.memory_ids) == 2 and res.node_count == 5
    assert _sha(settings.db_path) == h
    with pytest.raises(ValidationFailedError):
        TopologyService(settings.db_path).order(limit=0)


def test_cli_graph_order(svc, settings, engine, capsys) -> None:
    a = svc.create_memory(make(content="SECRET-BODY"), USER).memory_id
    b = svc.create_memory(make(parent_ids=[a]), USER).memory_id
    engine.dispose()
    rc = main(["graph-order", "--db", str(settings.db_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and "SECRET-BODY" not in out
    assert json.loads(out)["memory_ids"] == [a, b]
    rc = main(["graph-order", "--db", str(settings.db_path), "--edge-type", "PARENT"])
    assert rc == 0 and f"1\t{a}" in capsys.readouterr().out
    assert main(["graph-order", "--db", str(settings.db_path), "--limit", "0"]) == 3
    with pytest.raises(SystemExit) as ei:
        main(["graph-order", "--edge-type", "BOGUS"])
    assert ei.value.code == 64


def test_cli_space(settings, engine, capsys) -> None:
    engine.dispose()
    db = str(settings.db_path)
    assert main(["space", "--db", db, "create", "project:y", "--scope-type", "project"]) == 0
    assert main(["space", "--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert "project:y\tproject" in out and "shared\tshared" in out
    assert main(["space", "--db", db, "create", "project:y", "--scope-type", "project"]) == 3

import json
from pathlib import Path

from sqlalchemy import inspect

from ame.application.schemas import (
    EdgeCreate,
    EdgeRead,
    MemoryCreate,
    MemoryRead,
    RelationCreate,
    RelationRead,
    StatusChange,
    VersionCreate,
    VersionRead,
)
from tests.conftest import AGENT, make

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
FORBIDDEN = {"id", "source_id_edge", "target_id", "parent_id", "mem_id", "version_no"}


def test_db_columns_use_canonical_names(engine):
    insp = inspect(engine)
    cols = {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}
    assert {"memory_id", "current_version"} <= cols["memories"]
    assert {"version_id", "memory_id", "version"} <= cols["memory_versions"]
    assert {"edge_id", "from_memory_id", "to_memory_id"} <= cols["memory_edges"]
    assert "relation_id" in cols["memory_relations"]
    # FTS5 shadow tables (memory_fts_*) are owned by SQLite itself, not our schema (ADR-020).
    for t, names in cols.items():
        if t.startswith("memory_fts_"):
            continue
        assert "id" not in names, f"bare 'id' column in {t}"


def test_rest_dto_matches_db_names(engine, svc):
    m = svc.create_memory(make(), AGENT)
    dumped = m.model_dump()
    assert dumped["current_version"] == 1 and dumped["memory_id"].startswith("mem_")
    assert {"memory_id", "current_version", "version_id"} <= set(MemoryRead.model_fields)
    assert {"version_id", "memory_id", "version"} <= set(VersionRead.model_fields)
    assert "edge_id" in EdgeRead.model_fields and "relation_id" in RelationRead.model_fields
    for model in (MemoryRead, VersionRead, EdgeRead, RelationRead):
        assert not (FORBIDDEN & set(model.model_fields))


def test_json_schemas_match_dtos():
    from tools.gen_schemas import TARGETS, render

    for name, model in TARGETS.items():
        assert (SCHEMAS / name).read_text(encoding="utf-8") == render(model), name
    mem = json.loads((SCHEMAS / "memory.schema.json").read_text(encoding="utf-8"))
    assert set(mem["properties"]) == set(MemoryRead.model_fields)


def test_mcp_contract_uses_same_field_names():
    tools = json.loads((SCHEMAS / "mcp_tools.json").read_text(encoding="utf-8"))["tools"]
    assert set(tools["memory_write"]["input"]) == set(MemoryCreate.model_fields) | {
        "idempotency_key"
    }
    assert set(tools["memory_create_version"]["input"]) == set(VersionCreate.model_fields) | {
        "memory_id"
    }
    assert set(tools["memory_link"]["input"]) == set(EdgeCreate.model_fields)
    assert set(tools["memory_relate"]["input"]) == set(RelationCreate.model_fields)
    assert set(tools["memory_change_status"]["input"]) == set(StatusChange.model_fields) | {
        "memory_id"
    }
    from ame.application.schemas import LinkRevoke

    for tool, pk in (("memory_revoke_edge", "edge_id"), ("memory_revoke_relation", "relation_id")):
        assert set(tools[tool]["input"]) == set(LinkRevoke.model_fields) | {pk}
    assert tools["memory_read"]["output"] == "memory.schema.json"
    for spec in tools.values():
        assert not (FORBIDDEN & set(spec["input"]))

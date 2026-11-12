import json

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from tests.conftest import AGENT, make


def test_tags_sources_parents_refs_stored_separately(svc, engine):
    p = svc.create_memory(make(content="parent"), AGENT).memory_id
    r = svc.create_memory(make(content="ref"), AGENT).memory_id
    m = svc.create_memory(
        make(
            tags=["win", "capcut", "win"],
            parent_ids=[p],
            reference_ids=[r],
            sources=[{"source_type": "file", "source_ref": "a.txt", "excerpt": "x"}],
            metadata={"ui_color": "blue"},
        ),
        AGENT,
    )
    assert m.tags == ["capcut", "win"]
    assert m.parent_ids == [p] and m.reference_ids == [r]
    assert m.sources[0].source_ref == "a.txt"
    assert svc.list_memory_ids_by_tag("win") == [m.memory_id]
    with engine.connect() as c:
        q = lambda s: c.execute(text(s), {"m": m.memory_id}).all()  # noqa: E731
        assert len(q("SELECT * FROM memory_tags WHERE memory_id=:m")) == 2
        assert len(q("SELECT * FROM memory_sources WHERE memory_id=:m")) == 1
        edges = q(
            "SELECT from_memory_id, to_memory_id, edge_type FROM memory_edges "
            "WHERE from_memory_id=:m OR to_memory_id=:m ORDER BY edge_type"
        )
        assert [tuple(e) for e in edges] == [
            (p, m.memory_id, "PARENT"),
            (m.memory_id, r, "REFERENCE"),
        ]
        meta = json.loads(q("SELECT metadata_json FROM memories WHERE memory_id=:m")[0][0])
    assert meta == {"ui_color": "blue"}


@pytest.mark.parametrize(
    "key", ["tags", "parent_ids", "reference_ids", "sources", "status", "layer"]
)
def test_metadata_rejects_core_fields(key):
    with pytest.raises(ValidationError):
        make(metadata={key: "x"})


def test_unknown_parent_rejected(svc):
    from ame.errors import NotFoundError

    with pytest.raises(NotFoundError):
        svc.create_memory(make(parent_ids=["mem_missing"]), AGENT)

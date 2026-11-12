import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from ame.application.schemas import VersionCreate
from ame.errors import VersionConflictError
from tests.conftest import AGENT, USER, make


def test_new_version_keeps_history(svc):
    m = svc.create_memory(make(content="v1"), AGENT)
    v2 = svc.create_version(
        m.memory_id, VersionCreate(content="v2", change_reason="fix", expected_version=1), USER
    )
    assert v2.version == 2
    cur = svc.get_memory(m.memory_id)
    assert cur.current_version == 2 and cur.content == "v2" and cur.status == m.status
    assert [v.content for v in svc.list_versions(m.memory_id)] == ["v1", "v2"]


def test_stale_expected_version_conflicts(svc):
    m = svc.create_memory(make(), AGENT)
    vc = VersionCreate(content="a", change_reason="r", expected_version=1)
    svc.create_version(m.memory_id, vc, USER)
    with pytest.raises(VersionConflictError):
        svc.create_version(m.memory_id, vc, AGENT)
    assert len(svc.list_versions(m.memory_id)) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE memory_versions SET content='hacked'",
        "DELETE FROM memory_versions",
        "UPDATE audit_events SET reason='x'",
        "DELETE FROM audit_events",
    ],
)
def test_history_tables_are_append_only(svc, engine, sql):
    svc.create_memory(make(content="orig"), AGENT)
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as c:
        c.execute(text(sql))
    with engine.connect() as c:
        assert c.execute(text("SELECT content FROM memory_versions")).scalar() == "orig"


def test_legacy_audit_table_is_frozen(svc, engine):
    """Q-D1: the 0002 memory_audit_events table is kept read-only, never dropped."""
    m = svc.create_memory(make(), AGENT)
    with pytest.raises(DatabaseError, match="frozen"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO memory_audit_events (audit_id, memory_id, event_type, from_value, "
                'to_value, "trigger", actor_type, actor_id, reason, created_at) VALUES '
                "('a1', :m, 'created', NULL, 'candidate', 'T1', 'agent', 'x', 'r', 't')"
            ),
            {"m": m.memory_id},
        )

"""Names of DB guard triggers expected at each schema revision (used by check-graph G007
and by the migration runner's post-check). Trigger SQL lives only in the migrations."""

from __future__ import annotations

REVISIONS = (
    "0001_baseline",
    "0002_core_memory_tables",
    "0003_link_revocation",
    "0004_dag_integrity",
    "0005_conflicts_audit",
    "0006_agents_spaces",
    "0007_search_outbox",
    "0008_cjk_compact_index",
    "0009_audit_registry",
    "0010_space_acl",
    "0011_idempotency",
    "0012_agent_key_index",
    "0013_feedback",
    "0014_epochs",
    "0015_distill_jobs",
    "0016_single_parent",
)
HEAD = REVISIONS[-1]

TRIGGERS_BY_REVISION: dict[str, tuple[str, ...]] = {
    "0002_core_memory_tables": (
        "trg_memory_versions_no_update",
        "trg_memory_versions_no_delete",
        "trg_memory_audit_events_no_update",
        "trg_memory_audit_events_no_delete",
    ),
    "0003_link_revocation": (
        "trg_memory_edges_revoke_only",
        "trg_memory_edges_no_delete",
        "trg_memory_relations_revoke_only",
        "trg_memory_relations_no_delete",
        "trg_memory_versions_archived_guard",
        "trg_memories_archived_version_guard",
    ),
    "0004_dag_integrity": (
        "trg_memory_edges_no_cycle_insert",
        "trg_memory_edges_no_cycle_update",
        "trg_memory_edges_endpoints_insert",
        "trg_memory_relations_endpoints_insert",
    ),
    "0005_conflicts_audit": (
        "trg_audit_events_no_update",
        "trg_audit_events_no_delete",
        "trg_memory_audit_events_frozen",
        "trg_memory_conflicts_no_delete",
        "trg_memory_conflicts_resolve_only",
        "trg_memory_conflicts_open_insert",
        "trg_memories_conflicted_requires_evidence",
        "trg_memories_conflicted_insert_guard",
        "trg_memory_edges_frozen_endpoint",
        "trg_memory_relations_frozen_endpoint",
    ),
    "0006_agents_spaces": (
        "trg_memories_space_insert",
        "trg_memories_space_update",
        "trg_spaces_no_delete",
        "trg_spaces_identity_immutable",
        "trg_agents_no_delete",
    ),
    "0007_search_outbox": (
        "trg_memory_versions_fts_sync",
        "trg_memory_tags_fts_insert",
        "trg_memory_tags_fts_delete",
        "trg_memory_versions_outbox",
        "trg_memories_status_outbox",
        "trg_outbox_events_no_delete",
    ),
    "0008_cjk_compact_index": ("trg_memory_versions_fts_compact_sync",),
    "0010_space_acl": ("trg_space_members_no_delete",),
    "0013_feedback": ("trg_memory_feedback_no_update", "trg_memory_feedback_no_delete"),
    "0014_epochs": (
        "trg_epochs_no_delete",
        "trg_epochs_closed_final",
        "trg_epoch_members_no_delete",
        "trg_epoch_members_closed_insert",
        "trg_epoch_members_closed_update",
    ),
    "0015_distill_jobs": ("trg_distill_jobs_no_update", "trg_distill_jobs_no_delete"),
    "0016_single_parent": ("trg_memory_edges_single_parent",),
}


def required_triggers(revision: str | None) -> list[str]:
    if revision not in REVISIONS:
        return []
    upto = REVISIONS[: REVISIONS.index(revision) + 1]
    return [t for r in upto for t in TRIGGERS_BY_REVISION.get(r, ())]

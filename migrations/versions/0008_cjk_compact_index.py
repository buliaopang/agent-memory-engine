"""Whitespace-insensitive lexical index for CJK text (ADR-023, DECISIONS C21-C26).
Incremental; 0001-0007 untouched.

- memory_fts_compact: FTS5 trigram table holding the CURRENT version's content/summary with
  ASCII space, U+3000 full-width space, TAB, LF and CR removed, so '部署流程' matches text
  written '部署 流程'. Tags are NOT copied (memory_fts already indexes them; joining tags
  without separators would create false matches). Punctuation is kept (C23).
- Derived data, rebuildable from memory_versions (`ame index rebuild-lexical`).
- Kept in sync by one trigger on new current versions (same condition as 0007).
- Back-fill for all existing memories; re-records trigger fingerprints (G007).
- Downgrade drops only the derived table + trigger (no source data is touched).

Revision ID: 0008_cjk_compact_index
Revises: 0007_search_outbox
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0008_cjk_compact_index"
down_revision = "0007_search_outbox"
branch_labels = None
depends_on = None

# Must stay identical to ame.domain.text_normalize.compact() (tested both ways).
_STRIP = ("' '", "char(12288)", "char(9)", "char(10)", "char(13)")


def compact_sql(expr: str) -> str:
    for ch in _STRIP:
        expr = f"replace({expr}, {ch}, '')"
    return expr


_IS_CURRENT = "NEW.version = (SELECT current_version FROM memories WHERE memory_id = NEW.memory_id)"

TRIGGERS = {
    "trg_memory_versions_fts_compact_sync": (
        "CREATE TRIGGER trg_memory_versions_fts_compact_sync AFTER INSERT ON memory_versions "
        f"WHEN {_IS_CURRENT} BEGIN "
        "DELETE FROM memory_fts_compact WHERE memory_id = NEW.memory_id; "
        "INSERT INTO memory_fts_compact (memory_id, version, content, summary) VALUES "
        f"(NEW.memory_id, NEW.version, {compact_sql('NEW.content')}, "
        + compact_sql("COALESCE(NEW.summary, '')")
        + "); END"
    ),
}

BACKFILL = (
    "INSERT INTO memory_fts_compact (memory_id, version, content, summary) "
    f"SELECT m.memory_id, v.version, {compact_sql('v.content')}, "
    + compact_sql("COALESCE(v.summary, '')")
    + " "
    "FROM memories m JOIN memory_versions v ON v.memory_id = m.memory_id "
    "AND v.version = m.current_version ORDER BY m.memory_id"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def record_fingerprints(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
    ).fetchall()
    fp = {n: hashlib.sha256(" ".join(s.split()).encode()).hexdigest() for n, s in rows}
    bind.execute(
        sa.text(
            "INSERT INTO ame_meta(key, value, updated_at) VALUES ('trigger_fingerprints', :v, :t) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at"
        ),
        {"v": json.dumps(fp, sort_keys=True), "t": _now()},
    )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        "CREATE VIRTUAL TABLE memory_fts_compact USING fts5("
        "memory_id UNINDEXED, version UNINDEXED, content, summary, tokenize = 'trigram')"
    )
    bind.execute(sa.text(BACKFILL))
    for sql in TRIGGERS.values():
        op.execute(sql)
    record_fingerprints(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS memory_fts_compact")
    record_fingerprints(bind)

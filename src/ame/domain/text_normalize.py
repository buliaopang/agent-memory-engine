"""Whitespace compaction for CJK-friendly lexical matching (ADR-023, DECISIONS C21-C23).

Removes exactly: ASCII space, U+3000 (full-width space), TAB, LF, CR. Punctuation is kept.
Must stay identical to the SQL expression used by migration 0008 and rebuild_lexical."""

from __future__ import annotations

STRIP_CHARS = (" ", "\u3000", "\t", "\n", "\r")
_TABLE = {ord(c): None for c in STRIP_CHARS}


def compact(text: str) -> str:
    return text.translate(_TABLE)


def compact_sql(expr: str) -> str:
    """SQLite expression equivalent to compact(expr)."""
    for ch in STRIP_CHARS:
        expr = f"replace({expr}, char({ord(ch)}), '')"
    return expr

"""Derived vector stores (ADR-021). They hold ids, version, model, hash and vectors only -
NEVER memory text. They can be dropped and rebuilt from SQLite at any time.

- InMemoryVectorStore: dependency-free, process-local (tests / ephemeral use).
- LanceDBVectorStore: optional extra `ame[vector]`; imported lazily so AME works without it.
Similarity = cosine similarity clamped to 0..1 (computed, never invented)."""

from __future__ import annotations

import importlib
import math
import re
import threading
from pathlib import Path
from typing import Any

from ame.application.ports import VectorHit, VectorRecord, VectorUnavailableError

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise VectorUnavailableError("dimension mismatch")
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)))


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._rows: dict[str, VectorRecord] = {}
        self._lock = threading.Lock()

    def upsert(self, record: VectorRecord) -> None:
        with self._lock:
            self._rows[record.memory_id] = record

    def delete(self, memory_id: str) -> None:
        with self._lock:
            self._rows.pop(memory_id, None)

    def search(self, vector: list[float], k: int, allowed: set[str]) -> list[VectorHit]:
        with self._lock:
            rows = [r for r in self._rows.values() if r.memory_id in allowed]
        hits = [
            VectorHit(r.memory_id, r.version, r.embedding_model, cosine(vector, r.vector))
            for r in rows
        ]
        hits.sort(key=lambda h: (-h.similarity, h.memory_id))
        return hits[:k]

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()

    def count(self) -> int:
        return len(self._rows)

    def health(self) -> dict[str, Any]:
        return {"backend": "memory", "ok": True, "rows": self.count()}


class LanceDBVectorStore:
    """Optional; requires `pip install ame[vector]`. Table columns: memory_id, version,
    embedding_model, embedding_dimension, content_hash, indexed_at, vector. No text."""

    TABLE = "memory_vectors"

    def __init__(self, path: Path, dimension: int) -> None:
        try:
            lancedb = importlib.import_module("lancedb")
            pa = importlib.import_module("pyarrow")
        except ImportError as exc:  # optional extra not installed
            raise VectorUnavailableError("lancedb not installed") from exc
        self._pa = pa
        self._dim = dimension
        self._db = lancedb.connect(str(path))
        self._schema = pa.schema(
            [
                ("memory_id", pa.string()),
                ("version", pa.int64()),
                ("embedding_model", pa.string()),
                ("embedding_dimension", pa.int64()),
                ("content_hash", pa.string()),
                ("indexed_at", pa.string()),
                ("vector", pa.list_(pa.float32(), dimension)),
            ]
        )
        self._table = self._open()

    def _open(self) -> Any:
        if self.TABLE in self._names():
            return self._db.open_table(self.TABLE)
        return self._db.create_table(self.TABLE, schema=self._schema)

    def _names(self) -> list[str]:
        res = self._db.list_tables()
        names = getattr(res, "tables", res)
        return [str(n) for n in names]

    def upsert(self, record: VectorRecord) -> None:
        if record.embedding_dimension != self._dim:
            raise VectorUnavailableError("dimension mismatch")
        row = {
            "memory_id": record.memory_id,
            "version": record.version,
            "embedding_model": record.embedding_model,
            "embedding_dimension": record.embedding_dimension,
            "content_hash": record.content_hash,
            "indexed_at": record.indexed_at,
            "vector": record.vector,
        }
        (
            self._table.merge_insert("memory_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute([row])
        )

    def delete(self, memory_id: str) -> None:
        if not _ID_RE.match(memory_id):
            raise ValueError("invalid memory_id")
        self._table.delete(f"memory_id = '{memory_id}'")

    def search(self, vector: list[float], k: int, allowed: set[str]) -> list[VectorHit]:
        if not allowed:
            return []
        # over-fetch then filter by the SQLite-computed allow-list (filter-before-use)
        rows = self._table.search(vector).distance_type("cosine").limit(k * 4 + 20).to_list()
        hits = [
            VectorHit(
                str(r["memory_id"]),
                int(r["version"]),
                str(r["embedding_model"]),
                max(0.0, min(1.0, 1.0 - float(r["_distance"]))),
            )
            for r in rows
            if r["memory_id"] in allowed
        ]
        hits.sort(key=lambda h: (-h.similarity, h.memory_id))
        return hits[:k]

    def clear(self) -> None:
        if self.TABLE in self._names():
            self._db.drop_table(self.TABLE)
        self._table = self._open()

    def count(self) -> int:
        return int(self._table.count_rows())

    def health(self) -> dict[str, Any]:
        try:
            return {"backend": "lancedb", "ok": True, "rows": self.count()}
        except Exception as exc:
            return {"backend": "lancedb", "ok": False, "error": type(exc).__name__}

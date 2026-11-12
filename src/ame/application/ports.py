"""Ports for the derived vector index (ADR-021/022). SQLite stays the single source of truth;
implementations hold ids, versions, model info and vectors only - never memory text."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class VectorRecord:
    memory_id: str
    version: int
    embedding_model: str
    embedding_dimension: int
    content_hash: str
    indexed_at: str
    vector: list[float]


@dataclass(frozen=True)
class VectorHit:
    memory_id: str
    version: int
    embedding_model: str
    similarity: float  # cosine similarity clamped to 0..1, as computed by the store


class VectorStore(Protocol):
    def upsert(self, record: VectorRecord) -> None: ...

    def delete(self, memory_id: str) -> None: ...

    def search(self, vector: list[float], k: int, allowed: set[str]) -> list[VectorHit]: ...

    def clear(self) -> None: ...

    def count(self) -> int: ...

    def health(self) -> dict[str, Any]: ...


class VectorUnavailableError(Exception):
    """The vector side failed; callers degrade to lexical_graph (never fake a score)."""

"""Local embedding providers and optional VectorBackend factory (ADR-022, ADR-034).

- Cloud embedding APIs are never allowed (SECURITY.md #10).
- ``embedding_provider="none"`` (default): returns ``None`` -> ``lexical_graph`` mode.
- ``embedding_provider="fastembed"``: uses local ONNX inference via optional ``fastembed``
  (``pip install ame[vector]``). If ``fastembed`` or the configured store extra is not
  installed, ``build_vector_backend`` logs a warning and returns ``None`` so AME degrades
  truthfully to ``lexical_graph`` mode (Q17).
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Sequence

import structlog

from ame.application.ports import VectorStore, VectorUnavailableError
from ame.application.search_service import VectorBackend
from ame.config import Settings
from ame.infrastructure.vector_stores import InMemoryVectorStore, LanceDBVectorStore

log = structlog.get_logger()

DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"


class FastEmbedProvider:
    """Local ONNX embedding provider via optional ``fastembed`` package."""

    def __init__(
        self,
        model_name: str = DEFAULT_FASTEMBED_MODEL,
        hf_endpoint: str | None = None,
    ) -> None:
        try:
            fe = importlib.import_module("fastembed")
        except ImportError as exc:
            raise VectorUnavailableError("fastembed is not installed") from exc

        if hf_endpoint:
            os.environ["HF_ENDPOINT"] = hf_endpoint
        elif not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = DEFAULT_HF_ENDPOINT

        try:
            text_embedding_cls = fe.TextEmbedding
            self._model = text_embedding_cls(model_name=model_name)
            probe = list(next(iter(self._model.embed(["dimension probe"]))))
        except Exception as exc:
            raise VectorUnavailableError(
                f"fastembed model initialization failed: {type(exc).__name__}"
            ) from exc

        self._model_name = model_name
        self._dimension = len(probe)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return [[float(x) for x in vec] for vec in self._model.embed(list(texts))]
        except Exception as exc:
            raise VectorUnavailableError(f"fastembed embed failed: {type(exc).__name__}") from exc


def build_vector_backend(settings: Settings) -> VectorBackend | None:
    """Construct the local ``VectorBackend`` if configured and available, else ``None``."""
    if settings.embedding_provider == "none":
        return None
    try:
        provider = FastEmbedProvider(
            model_name=settings.embedding_model,
            hf_endpoint=settings.hf_endpoint or None,
        )
        store: VectorStore
        if settings.vector_store_backend == "memory":
            store = InMemoryVectorStore()
        else:
            store = LanceDBVectorStore(settings.data_dir / "lancedb", provider.dimension)
        return VectorBackend(provider=provider, store=store)
    except VectorUnavailableError as exc:
        log.warning(
            "vector_backend_unavailable",
            provider=settings.embedding_provider,
            store=settings.vector_store_backend,
            reason=str(exc),
        )
        return None

"""Test-only deterministic embedding (P2-1: never configurable in the product)."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from ame.application.ports import VectorUnavailableError


class HashingEmbeddingProvider:
    """Bag of character bigrams hashed into `dimension` buckets, L2-normalised."""

    model_name = "test-hashing-v1"

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dimension
            s = t.lower()
            for i in range(max(1, len(s) - 1)):
                h = hashlib.sha256(s[i : i + 2].encode()).digest()
                v[int.from_bytes(h[:4], "big") % self.dimension] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class BrokenProvider(HashingEmbeddingProvider):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise VectorUnavailableError("model missing")

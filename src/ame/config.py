"""Configuration. All paths use pathlib for Windows compatibility."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ame.domain.phase4_scoring import SpineWeights, parse_spine_weights
from ame.domain.ranking import Weights


def default_data_dir() -> Path:
    """%LOCALAPPDATA%\\AME on Windows, ~/.local/share/ame elsewhere."""
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        return Path(local) / "AME"
    return Path.home() / ".local" / "share" / "ame"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AME_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Field(default_factory=default_data_dir)
    db_filename: str = "ame.db"
    log_level: str = "INFO"
    diagnostic_mode: bool = False
    # Phase 2 search ranking (ADR-020). Must sum to 1.0; validated at startup.
    search_weight_vector: float = 0.35
    search_weight_graph: float = 0.20
    search_weight_importance: float = 0.15
    search_weight_confidence: float = 0.20
    search_weight_freshness: float = 0.10
    search_freshness_half_life_days: float = Field(default=30.0, gt=0)
    # Outbox worker (ADR-021). Runs inside the server process.
    index_worker_enabled: bool = True
    index_worker_interval_s: float = Field(default=2.0, gt=0)
    # Embedding (ADR-022, ADR-034): local-only; "none" (default) or "fastembed" (ONNX).
    embedding_provider: str = "none"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    hf_endpoint: str = ""
    vector_store_backend: str = "lancedb"
    # Idempotency-Key replay records (ADR-027): kept 24 h by default, then purged.
    idempotency_ttl_hours: float = Field(default=24.0, gt=0)
    # Phase 3c MCP adapter (ADR-028). Off by default; read-only until the user opens writes.
    mcp_enabled: bool = False
    mcp_read_only: bool = True
    mcp_max_content_chars: int = Field(default=4000, ge=200, le=100_000)
    auto_approve_agent_writes: bool = False
    # Phase 4 (ADR-029/031/032). Spine weights: "confirmed=..,sources=..,feedback=..,
    # importance=..,freshness=.." (must sum to 1.0; validated at startup).
    spine_weights: str = ""
    spine_freshness_half_life_days: float = Field(default=90.0, gt=0)
    decay_half_life_days: float = Field(default=90.0, gt=0)
    decay_threshold: float = Field(default=0.15, ge=0, le=1)
    decay_protect_importance: float = Field(default=0.8, ge=0, le=1)
    distill_threshold: float = Field(default=0.5, gt=0, le=1)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # SECURITY.md #1: default bind 127.0.0.1; LAN/public bind is not allowed in Phase 0.
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("AME only binds to loopback addresses")
        return v

    @field_validator("embedding_provider")
    @classmethod
    def _local_provider_only(cls, v: str) -> str:
        if v not in {"none", "fastembed"}:
            raise ValueError(
                "only local embedding_provider in {'none', 'fastembed'} is supported; "
                "cloud providers are forbidden (ADR-022, ADR-034)"
            )
        return v

    @field_validator("vector_store_backend")
    @classmethod
    def _valid_vector_store(cls, v: str) -> str:
        if v not in {"lancedb", "memory"}:
            raise ValueError("vector_store_backend must be 'lancedb' or 'memory'")
        return v

    @model_validator(mode="after")
    def _weights_valid(self) -> Settings:
        self.search_weights()
        self.spine_weight_values()
        return self

    def spine_weight_values(self) -> SpineWeights:
        return parse_spine_weights(self.spine_weights)

    def search_weights(self) -> Weights:
        return Weights(
            vector=self.search_weight_vector,
            graph=self.search_weight_graph,
            importance=self.search_weight_importance,
            confidence=self.search_weight_confidence,
            freshness=self.search_weight_freshness,
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.resolve().as_posix()}"

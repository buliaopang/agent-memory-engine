"""Regenerate schemas/*.schema.json from the Pydantic DTOs (single source of truth)."""

from __future__ import annotations

import json
from pathlib import Path

from ame.application.schemas import (
    EdgeRead,
    MemoryRead,
    RelationRead,
    SearchRequest,
    SearchResponse,
    VersionRead,
)

ROOT = Path(__file__).resolve().parents[1] / "schemas"
TARGETS = {
    "memory.schema.json": MemoryRead,
    "version.schema.json": VersionRead,
    "edge.schema.json": EdgeRead,
    "relation.schema.json": RelationRead,
    "search-request.schema.json": SearchRequest,
    "search-response.schema.json": SearchResponse,
}


def render(model: type) -> str:
    schema = model.model_json_schema()  # type: ignore[attr-defined]
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> None:
    for name, model in TARGETS.items():
        (ROOT / name).write_text(render(model), encoding="utf-8")


if __name__ == "__main__":
    main()

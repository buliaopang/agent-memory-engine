"""Build a deliberately broken AME database for acceptance W08 (drops the cycle trigger,
then inserts a cycle). Usage: python scripts/make_broken_db.py <path-to-new.db>"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ame.infrastructure.migration_runner import MigrationRunner  # noqa: E402
from tests.phase11.dbutil import add_memory, connect  # noqa: E402

EDGE = (
    "INSERT INTO memory_edges (edge_id, from_memory_id, to_memory_id, edge_type, status, "
    "created_by, created_at, metadata_json) VALUES (?, ?, ?, ?, 'active', 'u', 't', '{}')"
)


def main(path: Path) -> None:
    MigrationRunner(path).upgrade()
    c = connect(path)
    for m in ("a", "b", "c"):
        add_memory(c, m)
    c.execute(EDGE, ("e1", "a", "b", "PARENT"))
    c.execute("DROP TRIGGER trg_memory_edges_no_cycle_insert")
    c.execute(EDGE, ("e2", "b", "a", "REFERENCE"))
    c.close()
    print(f"broken database created: {path}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

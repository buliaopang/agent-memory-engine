"""Seed a few Chinese memories for Windows acceptance W34-W36 (Phase 4). Writes as the local
user through MemoryService; prints only memory ids. Usage: python scripts/phase4_seed.py <db>"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ame.application.memory_service import MemoryService  # noqa: E402
from ame.application.schemas import Actor, MemoryCreate  # noqa: E402
from ame.config import Settings  # noqa: E402
from ame.domain.enums import ActorType  # noqa: E402
from ame.infrastructure.database import create_sqlite_engine, run_migrations  # noqa: E402
from ame.infrastructure.unit_of_work import make_session_factory  # noqa: E402

TEXTS = ("每天早上喝咖啡然后跑步五公里", "每天早上喝咖啡然后跑步三公里", "每天早上喝咖啡然后去跑步")


def main(db: Path) -> None:
    s = Settings(data_dir=db.parent, db_filename=db.name, index_worker_enabled=False)
    run_migrations(s)
    eng = create_sqlite_engine(s)
    svc = MemoryService(make_session_factory(eng))
    user = Actor(actor_type=ActorType.USER, actor_id="user:seed")
    for t in TEXTS:
        m = svc.create_memory(
            MemoryCreate.model_validate({"content": t, "memory_type": "semantic", "layer": "L2",
                                         "scope_type": "shared", "scope_id": "shared",
                                         "importance": 0.3, "confidence": 0.5,
                                         "tags": ["习惯"]}), user)  # fmt: skip
        print(m.memory_id)
    eng.dispose()


if __name__ == "__main__":
    main(Path(sys.argv[1]))

"""Phase 4 repositories: feedback, epochs, distill jobs, plus read helpers for spine/decay.
No update/delete methods for append-only tables (DB triggers enforce it as well)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ame.infrastructure.models import (
    DistillJob,
    Epoch,
    EpochMember,
    Memory,
    MemoryFeedback,
    MemorySource,
    MemoryTag,
)


class FeedbackRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, row: MemoryFeedback) -> None:
        self._s.add(row)
        self._s.flush()

    def list_for(self, memory_id: str) -> list[MemoryFeedback]:
        return list(
            self._s.scalars(
                select(MemoryFeedback)
                .where(MemoryFeedback.memory_id == memory_id)
                .order_by(MemoryFeedback.created_at, MemoryFeedback.feedback_id)
            )
        )

    def rows_for(self, memory_ids: list[str]) -> dict[str, list[tuple[str, str, str]]]:
        """memory_id -> [(actor_id, feedback, created_at)] ordered by time."""
        out: dict[str, list[tuple[str, str, str]]] = {}
        if not memory_ids:
            return out
        for i in range(0, len(memory_ids), 500):
            chunk = memory_ids[i : i + 500]
            q = (
                select(
                    MemoryFeedback.memory_id,
                    MemoryFeedback.actor_id,
                    MemoryFeedback.feedback,
                    MemoryFeedback.created_at,
                )
                .where(MemoryFeedback.memory_id.in_(chunk))
                .order_by(MemoryFeedback.created_at, MemoryFeedback.feedback_id)
            )
            for mid, actor, kind, at in self._s.execute(q):
                out.setdefault(mid, []).append((actor, kind, at))
        return out


class EpochRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, e: Epoch) -> None:
        self._s.add(e)
        self._s.flush()

    def get(self, epoch_id: str) -> Epoch | None:
        return self._s.get(Epoch, epoch_id)

    def list_all(self) -> list[Epoch]:
        return list(self._s.scalars(select(Epoch).order_by(Epoch.start_at, Epoch.epoch_id)))

    def member(self, epoch_id: str, memory_id: str) -> EpochMember | None:
        return self._s.get(EpochMember, (epoch_id, memory_id))

    def add_member(self, m: EpochMember) -> None:
        self._s.add(m)
        self._s.flush()

    def flush(self) -> None:
        self._s.flush()

    def active_member_ids(self, epoch_id: str) -> list[str]:
        return list(
            self._s.scalars(
                select(EpochMember.memory_id)
                .where(EpochMember.epoch_id == epoch_id, EpochMember.removed_at.is_(None))
                .order_by(EpochMember.memory_id)
            )
        )


class DistillRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, j: DistillJob) -> None:
        self._s.add(j)
        self._s.flush()

    def by_input(self, rule: str, input_key: str) -> DistillJob | None:
        return self._s.scalars(
            select(DistillJob).where(DistillJob.rule == rule, DistillJob.input_key == input_key)
        ).first()

    def by_output(self, memory_id: str) -> DistillJob | None:
        return self._s.scalars(
            select(DistillJob).where(DistillJob.output_memory_id == memory_id)
        ).first()

    def list_all(self) -> list[DistillJob]:
        return list(self._s.scalars(select(DistillJob).order_by(DistillJob.created_at)))


class Phase4ReadRepository:
    """Bulk reads for spine / decay / distill (one query per concern, no N+1)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def memories(self, scope_ids: Sequence[str] | None, statuses: Sequence[str]) -> list[Memory]:
        q = select(Memory).where(Memory.status.in_(statuses))
        if scope_ids is not None:
            q = q.where(Memory.scope_id.in_(scope_ids))
        return list(self._s.scalars(q.order_by(Memory.memory_id)))

    def source_counts(self) -> dict[str, int]:
        q = select(MemorySource.memory_id, func.count()).group_by(MemorySource.memory_id)
        return {mid: int(n) for mid, n in self._s.execute(q)}

    def tags_by_memory(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for mid, tag in self._s.execute(
            select(MemoryTag.memory_id, MemoryTag.tag).order_by(MemoryTag.tag)
        ):
            out.setdefault(mid, []).append(tag)
        return out

    def active_structural_edges(self, memory_ids: list[str]) -> list[tuple[str, str, str]]:
        """Active structural edges ``(from, to, edge_type)`` among ``memory_ids``, ordered by
        ``(created_at, edge_id)`` so parent-tree ties on legacy DBs are deterministic."""
        from ame.infrastructure.models import MemoryEdge
        from ame.infrastructure.search_repository import STRUCTURAL

        if not memory_ids:
            return []
        keep = set(memory_ids)
        rows = self._s.execute(
            select(MemoryEdge.from_memory_id, MemoryEdge.to_memory_id, MemoryEdge.edge_type)
            .where(MemoryEdge.status == "active", MemoryEdge.edge_type.in_(STRUCTURAL))
            .order_by(MemoryEdge.created_at, MemoryEdge.edge_id)
        )
        return [(str(a), str(b), str(t)) for a, b, t in rows if a in keep and b in keep and a != b]

    def active_relations(self, memory_ids: list[str]) -> list[tuple[str, str, str, str]]:
        """Active semantic relations ``(relation_id, from_memory_id, to_memory_id, relation_type)``
        among ``memory_ids``."""
        from ame.infrastructure.models import MemoryRelation

        if not memory_ids:
            return []
        keep = set(memory_ids)
        rows = self._s.execute(
            select(
                MemoryRelation.relation_id,
                MemoryRelation.from_memory_id,
                MemoryRelation.to_memory_id,
                MemoryRelation.relation_type,
            )
            .where(MemoryRelation.status == "active")
            .order_by(MemoryRelation.created_at, MemoryRelation.relation_id)
        )
        return [
            (str(rid), str(a), str(b), str(t))
            for rid, a, b, t in rows
            if a in keep and b in keep and a != b
        ]

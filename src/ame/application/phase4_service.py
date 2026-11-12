"""Phase 4 application services (ADR-029..032, PHASE_4_REVIEW D1-D12).

- FeedbackService: any actor may give feedback on a memory it can SEE (D11). Append-only.
- EpochService: only the user creates/closes epochs and changes members; agents read (D11).
- SpineService: read-only "main memories" ranking with per-item explanation (D6).
- DecayService: read-only archive SUGGESTIONS; only the user archives, one at a time (D5).
- DistillService / ReviewService: manual, rule-based drafts (no LLM, D3/D4) that are always
  candidate; only the user promotes or rejects them (D2), with full audit (D12).
Nothing here hard-deletes anything (D8). REST, CLI and MCP all call these services.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from ame.application import audit
from ame.application.access import Access, resolve
from ame.application.graph_service import GraphService
from ame.application.ids import new_id, sha256, utc_now
from ame.application.lifecycle import LifecycleService
from ame.application.schemas import (
    Actor,
    DecayArchive,
    DecayResponse,
    DecaySuggestion,
    DerivedEpochRead,
    DistillDraft,
    DistillRequest,
    DistillResult,
    EdgeCreate,
    EpochClose,
    EpochCreate,
    EpochMemberChange,
    EpochRead,
    FeedbackCreate,
    FeedbackRead,
    FeedbackSummary,
    MemoryCreate,
    MemoryRead,
    PromoteRequest,
    ReviewItem,
    SourceCreate,
    SpineItem,
    SpineResponse,
)
from ame.domain.conflux import ConfluxConsensus, compute_conflux_consensus
from ame.domain.enums import (
    ActorType,
    AuditEntityType,
    AuditEventType,
    EdgeType,
    Layer,
    MemoryStatus,
    MemoryType,
    ScopeType,
    SourceType,
)
from ame.domain.phase4_scoring import (
    SpineWeights,
    char_bigrams,
    clusters,
    decay_score,
    feedback_score,
    latest_per_actor,
    spine_components,
    weighted,
)
from ame.domain.ranking import freshness_score
from ame.errors import (
    EpochClosedError,
    InvalidTransitionError,
    NotFoundError,
    TransitionNotPermittedError,
    ValidationFailedError,
)
from ame.infrastructure.models import DistillJob, Epoch, EpochMember, Memory, MemoryFeedback
from ame.infrastructure.unit_of_work import UnitOfWork

DISTILL_RULE = "tag_bigram_v1"
DISTILL_ACTOR = Actor(actor_type=ActorType.SYSTEM, actor_id="system:distill")
DISTILL_TAG = "distill-draft"
EXCERPT_CHARS = 200
MAX_DISTILL_INPUTS = 20  # one draft never concatenates more than this many memories


def require_user(actor: Actor, what: str) -> None:
    if actor.actor_type is not ActorType.USER:
        raise TransitionNotPermittedError(f"Only the user may {what}", {"actor": actor.actor_type})


def _now() -> datetime:
    return datetime.now(UTC)


def _scopes(acc: Access, scope_id: str | None) -> list[str] | None:
    """None = no scope filter (user, no scope given). Invisible scopes -> empty list."""
    if scope_id is not None:
        return [scope_id] if acc.can_read(scope_id) else []
    return None if acc.readable is None else sorted(acc.readable)


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[: EXCERPT_CHARS - 1] + "…"


def feedback_read(f: MemoryFeedback) -> FeedbackRead:
    return FeedbackRead.model_validate({k: getattr(f, k) for k in FeedbackRead.model_fields})


class FeedbackService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def add(self, memory_id: str, cmd: FeedbackCreate, actor: Actor) -> FeedbackRead:
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            acc.see_memory(uow.memories.get(memory_id), memory_id)
            row = MemoryFeedback(
                feedback_id=new_id("fbk"),
                memory_id=memory_id,
                actor_type=actor.actor_type.value,
                actor_id=actor.actor_id,
                feedback=cmd.feedback.value,
                reason=cmd.reason,
                created_at=utc_now(),
            )
            uow.feedback.add(row)
            audit.record(
                uow, AuditEventType.FEEDBACK_ADDED, AuditEntityType.FEEDBACK, row.feedback_id,
                actor, cmd.reason, memory_id=memory_id, after={"feedback": cmd.feedback.value},
            )  # fmt: skip
            out = feedback_read(row)
            uow.commit()
            return out

    def summary(self, memory_id: str, actor: Actor | None = None) -> FeedbackSummary:
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            acc.see_memory(uow.memories.get(memory_id), memory_id)
            rows = uow.feedback.list_for(memory_id)
            latest = latest_per_actor((r.actor_id, r.feedback, r.created_at) for r in rows)
            counts = {k: 0 for k in ("useful", "not_useful", "outdated", "wrong")}
            for k in latest.values():
                counts[k] += 1
            return FeedbackSummary(
                memory_id=memory_id,
                counts=counts,
                voters=len(latest),
                score=round(feedback_score(latest), 6),
                items=[feedback_read(r) for r in rows],
            )


class EpochService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    @staticmethod
    def _read(uow: UnitOfWork, e: Epoch, acc: Access) -> EpochRead:
        ids = uow.epochs.active_member_ids(e.epoch_id)
        if acc.readable is not None:
            vis = []
            for i in ids:
                m = uow.memories.get(i)
                if m is not None and acc.can_read(m.scope_id):
                    vis.append(i)
            ids = vis
        return EpochRead(
            epoch_id=e.epoch_id,
            name=e.name,
            space_id=e.space_id,
            start_at=e.start_at,
            end_at=e.end_at,
            status=e.status,  # type: ignore[arg-type]
            created_by=e.created_by,
            created_at=e.created_at,
            metadata=json.loads(e.metadata_json),
            member_ids=ids,
        )

    @staticmethod
    def _see(uow: UnitOfWork, acc: Access, epoch_id: str) -> Epoch:
        e = uow.epochs.get(epoch_id)
        if e is None or (e.space_id is not None and not acc.can_read(e.space_id)):
            raise NotFoundError("Epoch not found", {"epoch_id": epoch_id})
        return e

    def create(self, cmd: EpochCreate, actor: Actor) -> EpochRead:
        require_user(actor, "create epochs")
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            if cmd.space_id is not None and uow.spaces.get(cmd.space_id) is None:
                raise NotFoundError("Space not found", {"space_id": cmd.space_id})
            now = utc_now()
            e = Epoch(
                epoch_id=new_id("epc"),
                name=cmd.name,
                space_id=cmd.space_id,
                start_at=cmd.start_at or now,
                end_at=None,
                status="open",
                created_by=actor.actor_id,
                created_at=now,
                metadata_json=json.dumps(cmd.metadata, ensure_ascii=False),
            )
            uow.epochs.add(e)
            audit.record(
                uow, AuditEventType.EPOCH_CREATED, AuditEntityType.EPOCH, e.epoch_id, actor,
                cmd.reason, after={"status": "open", "space_id": cmd.space_id},
            )  # fmt: skip
            out = self._read(uow, e, acc)
            uow.commit()
            return out

    def list(self, actor: Actor | None = None) -> list[EpochRead]:
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            return [
                self._read(uow, e, acc)
                for e in uow.epochs.list_all()
                if e.space_id is None or acc.can_read(e.space_id)
            ]

    def get(self, epoch_id: str, actor: Actor | None = None) -> EpochRead:
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            return self._read(uow, self._see(uow, acc, epoch_id), acc)

    def close(self, epoch_id: str, cmd: EpochClose, actor: Actor) -> EpochRead:
        require_user(actor, "close epochs")
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            e = self._see(uow, acc, epoch_id)
            if e.status != "open":
                raise EpochClosedError("Epoch is already closed", {"epoch_id": epoch_id})
            e.status, e.end_at = "closed", utc_now()
            uow.epochs.flush()
            audit.record(
                uow, AuditEventType.EPOCH_CLOSED, AuditEntityType.EPOCH, epoch_id, actor,
                cmd.reason, before={"status": "open"},
                after={"status": "closed", "end_at": e.end_at},
            )  # fmt: skip
            out = self._read(uow, e, acc)
            uow.commit()
            return out

    def add_member(self, epoch_id: str, cmd: EpochMemberChange, actor: Actor) -> EpochRead:
        require_user(actor, "change epoch members")
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            e = self._see(uow, acc, epoch_id)
            m = acc.see_memory(uow.memories.get(cmd.memory_id), cmd.memory_id)
            if e.status != "open":
                raise EpochClosedError("Epoch is closed", {"epoch_id": epoch_id})
            if e.space_id is not None and m.scope_id != e.space_id:
                raise ValidationFailedError(
                    "Memory belongs to another space than the epoch",
                    {"epoch_space_id": e.space_id, "memory_scope_id": m.scope_id},
                )
            row = uow.epochs.member(epoch_id, cmd.memory_id)
            now = utc_now()
            if row is not None and row.removed_at is None:
                return self._read(uow, e, acc)  # already a member: idempotent, no audit
            if row is None:
                uow.epochs.add_member(
                    EpochMember(
                        epoch_id=epoch_id,
                        memory_id=cmd.memory_id,
                        added_at=now,
                        added_by=actor.actor_id,
                        removed_at=None,
                        removed_by=None,
                    )  # fmt: skip
                )
            else:
                row.added_at, row.added_by = now, actor.actor_id
                row.removed_at, row.removed_by = None, None
                uow.epochs.flush()
            audit.record(
                uow, AuditEventType.EPOCH_MEMBER_ADDED, AuditEntityType.EPOCH, epoch_id, actor,
                cmd.reason, memory_id=cmd.memory_id, after={"member": True},
            )  # fmt: skip
            out = self._read(uow, e, acc)
            uow.commit()
            return out

    def remove_member(self, epoch_id: str, cmd: EpochMemberChange, actor: Actor) -> EpochRead:
        require_user(actor, "change epoch members")
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            e = self._see(uow, acc, epoch_id)
            if e.status != "open":
                raise EpochClosedError("Epoch is closed", {"epoch_id": epoch_id})
            row = uow.epochs.member(epoch_id, cmd.memory_id)
            if row is None or row.removed_at is not None:
                raise NotFoundError(
                    "Memory is not a member of this epoch",
                    {"epoch_id": epoch_id, "memory_id": cmd.memory_id},
                )
            row.removed_at, row.removed_by = utc_now(), actor.actor_id
            uow.epochs.flush()
            audit.record(
                uow, AuditEventType.EPOCH_MEMBER_REMOVED, AuditEntityType.EPOCH, epoch_id, actor,
                cmd.reason, memory_id=cmd.memory_id, before={"member": True},
                after={"member": False},
            )  # fmt: skip
            out = self._read(uow, e, acc)
            uow.commit()
            return out


def epoch_filter(uow: UnitOfWork, acc: Access, epoch_id: str | None) -> set[str] | None:
    """Active member ids of a visible epoch, or None when no epoch filter was requested."""
    if epoch_id is None:
        return None
    e = uow.epochs.get(epoch_id)
    if e is None or (e.space_id is not None and not acc.can_read(e.space_id)):
        raise NotFoundError("Epoch not found", {"epoch_id": epoch_id})
    return set(uow.epochs.active_member_ids(epoch_id))


def compute_consensus_in(
    uow: UnitOfWork,
    mems: list[Memory],
    weights: SpineWeights,
    half_life_days: float,
    now: datetime,
) -> tuple[
    list[tuple[float, str, Memory, dict[str, float], float]],
    dict[str, int],
    ConfluxConsensus,
]:
    """Compute node Spine scores and the Conflux consensus view over ``mems``."""
    ids = [m.memory_id for m in mems]
    fb_rows = uow.feedback.rows_for(ids)
    n_src = uow.p4.source_counts()
    scored: list[tuple[float, str, Memory, dict[str, float], float]] = []
    node_weights: dict[str, float] = {}
    for m in mems:
        fb = feedback_score(latest_per_actor(fb_rows.get(m.memory_id, [])))
        comp = spine_components(
            user_created=m.created_by.startswith("user:"),
            n_sources=n_src.get(m.memory_id, 0),
            fb=fb,
            importance=m.importance,
            freshness=freshness_score(m.updated_at, now, half_life_days),
        )
        sc = round(weighted(comp, weights), 6)
        node_weights[m.memory_id] = sc
        scored.append((sc, m.memory_id, m, comp, fb))
    edges = uow.p4.active_structural_edges(ids)
    consensus = compute_conflux_consensus(ids, node_weights, edges)
    return scored, n_src, consensus


class SpineService:
    """Read-only ranking + Conflux pivot chain, derived epochs and total order (ADR-031/033)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        weights: SpineWeights | None = None,
        half_life_days: float = 90.0,
    ) -> None:
        self._sf = session_factory
        self._w = weights or SpineWeights()
        self._half = half_life_days

    def spine(
        self,
        scope_id: str | None = None,
        limit: int = 20,
        epoch_id: str | None = None,
        actor: Actor | None = None,
        now: datetime | None = None,
    ) -> SpineResponse:
        now = now or _now()
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            scopes = _scopes(acc, scope_id)
            mems = [] if scopes == [] else uow.p4.memories(scopes, [MemoryStatus.ACTIVE.value])
            only = epoch_filter(uow, acc, epoch_id)
            if only is not None:
                mems = [m for m in mems if m.memory_id in only]
            scored, n_src, consensus = compute_consensus_in(uow, mems, self._w, self._half, now)
            w = self._w.as_dict()
            pivot_set = set(consensus.pivot_chain)
            scored.sort(key=lambda x: (-x[0], x[1]))
            items = []
            for score, mid, m, comp, fb in scored[:limit]:
                v = uow.versions.get(mid, m.current_version)
                why = [
                    f"confirmed {comp['confirmed']:.2f} x {w['confirmed']:.2f}"
                    + (" (created by the user)" if comp["confirmed"] == 1.0 else
                       " (written by an agent/system, approved by the user)"),
                    f"sources {comp['sources']:.2f} x {w['sources']:.2f} "
                    f"({n_src.get(mid, 0)} source(s), 3+ = full)",
                    f"feedback {comp['feedback']:.2f} x {w['feedback']:.2f} "
                    f"(latest-per-actor score {fb:+.2f})",
                    f"importance {comp['importance']:.2f} x {w['importance']:.2f}",
                    f"freshness {comp['freshness']:.2f} x {w['freshness']:.2f} "
                    f"(half-life {self._half:g} days)",
                ]  # fmt: skip
                items.append(
                    SpineItem(
                        memory_id=mid,
                        status=m.status,  # type: ignore[arg-type]
                        scope_id=m.scope_id,
                        summary=v.summary if v else None,
                        score=score,
                        components={k: round(x, 6) for k, x in comp.items()},
                        weights=w,
                        explanation=why,
                        parent_memory_id=consensus.parent_of.get(mid),
                        subtree_weight=consensus.subtree_weights.get(mid, score),
                        on_pivot_chain=mid in pivot_set,
                        epoch_index=consensus.epoch_of.get(mid),
                        total_order_rank=consensus.rank_of.get(mid),
                    )
                )
            return SpineResponse(
                total_candidates=len(scored),
                items=items,
                pivot_chain=list(consensus.pivot_chain),
                derived_epochs=[
                    DerivedEpochRead(
                        index=ep.index,
                        pivot_memory_id=ep.pivot_memory_id,
                        memory_ids=list(ep.memory_ids),
                    )
                    for ep in consensus.epochs
                ],
                total_order=list(consensus.total_order),
            )


DECAY_STATUSES = (
    MemoryStatus.ACTIVE.value,
    MemoryStatus.CANDIDATE.value,
    MemoryStatus.SUPERSEDED.value,
)


class DecayService:
    """Archive SUGGESTIONS only (D5). Never archives on its own; never deletes."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        half_life_days: float = 90.0,
        threshold: float = 0.15,
        protect_importance: float = 0.8,
    ) -> None:
        self._sf = session_factory
        self._half = half_life_days
        self._threshold = threshold
        self._protect = protect_importance
        self._life = LifecycleService(GraphService())

    def _evaluate(
        self, uow: UnitOfWork, mems: list[Memory], threshold: float, now: datetime
    ) -> list[DecaySuggestion]:
        fb_rows = uow.feedback.rows_for([m.memory_id for m in mems])
        out = []
        for m in mems:
            rows = fb_rows.get(m.memory_id, [])
            latest = latest_per_actor(rows)
            if m.importance >= self._protect:
                continue  # protected: the user marked it important
            if any(a.startswith("user:") and k == "useful" for a, k in latest.items()):
                continue  # protected: the user confirmed it is useful
            last = max([m.updated_at, *[r[2] for r in rows]])
            fb = feedback_score(latest)
            score, age = decay_score(m.importance, last, now, self._half, fb)
            if score >= threshold:
                continue
            out.append(
                DecaySuggestion(
                    memory_id=m.memory_id,
                    status=m.status,  # type: ignore[arg-type]
                    scope_id=m.scope_id,
                    score=round(score, 6),
                    age_days=round(age, 2),
                    importance=m.importance,
                    feedback=round(fb, 6),
                    explanation=[
                        f"importance {m.importance:.2f} x 0.5^({age:.1f} / {self._half:g} days)"
                        f" + 0.2 x feedback {fb:+.2f} = {score:.3f} < threshold {threshold:g}",
                        "suggestion only: nothing is archived until the user confirms",
                    ],
                )
            )
        out.sort(key=lambda s: (s.score, s.memory_id))
        return out

    def suggest(
        self,
        scope_id: str | None = None,
        threshold: float | None = None,
        limit: int = 50,
        actor: Actor | None = None,
        now: datetime | None = None,
    ) -> DecayResponse:
        now = now or _now()
        th = self._threshold if threshold is None else threshold
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            scopes = _scopes(acc, scope_id)
            mems = [] if scopes == [] else uow.p4.memories(scopes, list(DECAY_STATUSES))
            return DecayResponse(
                threshold=th,
                half_life_days=self._half,
                suggestions=self._evaluate(uow, mems, th, now)[:limit],
            )

    def archive_suggested(
        self, cmd: DecayArchive, actor: Actor, now: datetime | None = None
    ) -> MemoryRead:
        """User-only, one memory at a time, and only while it is still a suggestion."""
        from ame.application.memory_service import MemoryService

        require_user(actor, "archive from a decay suggestion")
        now = now or _now()
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            m = acc.see_memory(uow.memories.get(cmd.memory_id), cmd.memory_id)
            hit = (
                self._evaluate(uow, [m], self._threshold, now) if m.status in DECAY_STATUSES else []
            )
            if not hit:
                raise ValidationFailedError(
                    "Memory is not (or no longer) a decay suggestion",
                    {"memory_id": cmd.memory_id},
                )
            reason = f"decay suggestion score={hit[0].score:.3f}: {cmd.reason}"
            self._life.set_status(uow, m, MemoryStatus.ARCHIVED, actor, reason)
            out = MemoryService._read(uow, m, acc)
            uow.commit()
            return out


class DistillService:
    """Manual, deterministic distill (D3/D4): no LLM, no background job, drafts only."""

    def __init__(self, session_factory: sessionmaker[Session], threshold: float = 0.5) -> None:
        from ame.application.memory_service import MemoryService

        self._sf = session_factory
        self._threshold = threshold
        self._mem = MemoryService(session_factory)
        self._graph = GraphService()

    def run(self, req: DistillRequest, actor: Actor) -> DistillResult:
        require_user(actor, "run distill")
        th = self._threshold if req.threshold is None else req.threshold
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            space = uow.spaces.get(req.scope_id)
            if space is None:
                raise NotFoundError("Space not found", {"space_id": req.scope_id})
            mems = uow.p4.memories([req.scope_id], [MemoryStatus.ACTIVE.value])
            tags = uow.p4.tags_by_memory()
            text: dict[str, str] = {}
            hashes: dict[str, str] = {}
            groups: dict[str, list[str]] = {}
            for m in mems:
                mtags = [t for t in tags.get(m.memory_id, []) if t != DISTILL_TAG]
                if DISTILL_TAG in tags.get(m.memory_id, []):
                    continue  # never distill a draft again
                v = uow.versions.get(m.memory_id, m.current_version)
                if v is None:
                    continue
                text[m.memory_id], hashes[m.memory_id] = v.content, v.content_hash
                for t in mtags:
                    if req.tag is None or t == req.tag:
                        groups.setdefault(t, []).append(m.memory_id)
            grams = {mid: char_bigrams(t) for mid, t in text.items()}
            created: list[DistillDraft] = []
            seen: set[str] = set()
            skipped = 0
            for tag in sorted(groups):
                for cluster in clusters(groups[tag], grams, th):
                    cluster = cluster[:MAX_DISTILL_INPUTS]
                    key = sha256(",".join(cluster))
                    if key in seen or uow.distill.by_input(DISTILL_RULE, key) is not None:
                        skipped += 1
                        continue
                    seen.add(key)
                    created.append(
                        self._draft(
                            uow,
                            acc,
                            space.scope_type,
                            req,
                            actor,
                            tag,
                            cluster,
                            text,
                            hashes,
                            key,
                            th,
                        )  # fmt: skip
                    )
            uow.commit()
            return DistillResult(
                created=created, skipped_existing=skipped, groups_considered=len(groups)
            )

    def _draft(
        self,
        uow: UnitOfWork,
        acc: Access,
        scope_type: str,
        req: DistillRequest,
        actor: Actor,
        tag: str,
        ids: list[str],
        text: dict[str, str],
        hashes: dict[str, str],
        key: str,
        th: float,
    ) -> DistillDraft:
        job_id = new_id("dst")
        mems = [uow.memories.get(i) for i in ids]
        lines = [f"- [{i}] {_excerpt(text[i])}" for i in ids]
        content = (
            f"【蒸馏草稿 distill draft】规则 {DISTILL_RULE}，标签 #{tag}，来源 {len(ids)} 条。"
            "以下是原文摘录（未改写，原记忆保持不变）：\n" + "\n".join(lines)
        )
        cmd = MemoryCreate(
            content=content,
            summary=f"distill draft #{tag} ({len(ids)} sources)",
            memory_type=MemoryType.SEMANTIC,
            layer=Layer.L2,
            scope_type=ScopeType(scope_type),
            scope_id=req.scope_id,
            importance=max(m.importance for m in mems if m is not None),
            confidence=min(m.confidence for m in mems if m is not None),
            tags=[tag, DISTILL_TAG],
            sources=[
                SourceCreate(
                    source_type=SourceType.TOOL,
                    source_ref=f"ame://memory/{i}",
                    excerpt=_excerpt(text[i]),
                    content_hash=hashes[i],
                    metadata={"distill_job_id": job_id},
                )
                for i in ids
            ],
            metadata={"distill": {"job_id": job_id, "rule": DISTILL_RULE}},
        )
        out_id = self._mem.create_in(uow, acc, cmd, DISTILL_ACTOR)
        for i in ids:
            self._graph.add_edge(
                uow,
                EdgeCreate(from_memory_id=i, to_memory_id=out_id, edge_type=EdgeType.DERIVED_FROM),
                DISTILL_ACTOR,
            )
        uow.distill.add(
            DistillJob(
                job_id=job_id,
                rule=DISTILL_RULE,
                params_json=json.dumps({"tag": tag, "threshold": th}, sort_keys=True),
                input_ids_json=json.dumps(ids),
                input_key=key,
                output_memory_id=out_id,
                status="created",
                actor=actor.actor_id,
                created_at=utc_now(),
            )
        )
        audit.record(
            uow, AuditEventType.DISTILL_CREATED, AuditEntityType.DISTILL, job_id, actor,
            req.reason, memory_id=out_id,
            after={"rule": DISTILL_RULE, "tag": tag, "threshold": th, "source_memory_ids": ids,
                   "output_memory_id": out_id, "status": "candidate"},
        )  # fmt: skip
        return DistillDraft(job_id=job_id, memory_id=out_id, rule=DISTILL_RULE,
                            source_memory_ids=ids)  # fmt: skip


class ReviewService:
    """Candidate review queue; promote/reject are user-only (D2, D12)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory
        self._life = LifecycleService(GraphService())

    def list(self, scope_id: str | None = None, actor: Actor | None = None) -> list[ReviewItem]:
        with UnitOfWork(self._sf, read_only=True) as uow:
            acc = resolve(uow, actor)
            scopes = _scopes(acc, scope_id)
            mems = [] if scopes == [] else uow.p4.memories(scopes, [MemoryStatus.CANDIDATE.value])
            out = []
            for m in sorted(mems, key=lambda x: (x.created_at, x.memory_id)):
                job = uow.distill.by_output(m.memory_id)
                v = uow.versions.get(m.memory_id, m.current_version)
                out.append(
                    ReviewItem(
                        memory_id=m.memory_id,
                        scope_id=m.scope_id,
                        created_by=m.created_by,
                        created_at=m.created_at,
                        summary=v.summary if v else None,
                        is_distill_draft=job is not None,
                        source_memory_ids=json.loads(job.input_ids_json) if job else [],
                    )
                )
            return out

    def promote(self, memory_id: str, cmd: PromoteRequest, actor: Actor) -> MemoryRead:
        return self._decide(memory_id, cmd, actor, MemoryStatus.ACTIVE)

    def reject(self, memory_id: str, cmd: PromoteRequest, actor: Actor) -> MemoryRead:
        return self._decide(memory_id, cmd, actor, MemoryStatus.REJECTED)

    def _decide(
        self, memory_id: str, cmd: PromoteRequest, actor: Actor, to: MemoryStatus
    ) -> MemoryRead:
        from ame.application.memory_service import MemoryService

        require_user(actor, "promote or reject candidates")
        with UnitOfWork(self._sf) as uow:
            acc = resolve(uow, actor)
            m = acc.see_memory(uow.memories.get(memory_id), memory_id)
            if m.status != MemoryStatus.CANDIDATE.value:
                raise InvalidTransitionError(
                    "Only candidate memories can be promoted or rejected", {"status": m.status}
                )
            job = uow.distill.by_output(memory_id)
            sources = json.loads(job.input_ids_json) if job else []
            self._life.set_status(uow, m, to, actor, cmd.reason)
            etype = (
                AuditEventType.MEMORY_PROMOTED
                if to is MemoryStatus.ACTIVE
                else AuditEventType.MEMORY_REJECTED
            )
            audit.record(
                uow, etype, AuditEntityType.MEMORY, memory_id,
                actor, cmd.reason, memory_id=memory_id,
                before={"status": MemoryStatus.CANDIDATE.value},
                after={"status": to.value, "source_memory_ids": sources,
                       "distill_job_id": job.job_id if job else None},
            )  # fmt: skip
            out = MemoryService._read(uow, m, acc)
            uow.commit()
            return out

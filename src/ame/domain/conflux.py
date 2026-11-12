"""Conflux-style Tree-Graph consensus over the Structural DAG (ADR-033).

Pure, deterministic, no I/O, no LLM:
1. Parent tree: formed by active ``PARENT`` edges (parent -> child; at most one incoming
   ``PARENT`` per memory, enforced by migration 0016). If a legacy database has multiple
   incoming ``PARENT`` edges on a node, the earliest-ordered edge is used as the tree parent
   and the extras are treated as reference edges.
2. Pivot chain (heaviest-subtree Spine): each memory has its existing 5-factor Spine score as
   its node weight (always > 0). The weight of a parent-tree node is the sum of node weights in
   its parent-tree subtree. Starting from the heaviest root (ties broken by smaller
   ``memory_id``), the pivot chain repeatedly steps to the child with the largest subtree
   weight (ties broken by smaller ``memory_id``) until a leaf is reached.
3. Auto-derived Conflux epochs: for the i-th pivot memory ``p_i`` on the pivot chain, let
   ``Past(p_i)`` be all memories that can reach ``p_i`` along directed active structural edges
   (``PARENT``, ``REFERENCE``, ``SUPERSEDES``, ``DERIVED_FROM``; an edge ``u -> v`` means ``u``
   happened before ``v``), including ``p_i`` itself. The epoch of ``p_i`` is
   ``Past(p_i) - union_{j < i} Past(p_j)``. Any remaining memories not in ``Past(pivot_tip)``
   form a trailing ``unanchored`` epoch so every memory receives a position in the total order.
4. Total order: epochs are ordered along the pivot chain; inside each epoch, memories are
   topologically sorted over active structural edges, breaking ties by ``memory_id``.
5. Conflict suggestion: for a pair ``(a, b)``, whichever appears earlier in the total order is
   suggested as the first-arrived winner; the user still confirms the resolution.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ame.domain.topology import topological_order


@dataclass(frozen=True)
class DerivedEpoch:
    index: int  # 0-based along the pivot chain; len(pivot_chain) for the unanchored tail
    pivot_memory_id: str | None  # None for the unanchored tail
    memory_ids: tuple[str, ...]  # topologically sorted within the epoch


@dataclass(frozen=True)
class ConfluxConsensus:
    pivot_chain: tuple[str, ...]
    subtree_weights: dict[str, float]
    node_weights: dict[str, float]
    parent_of: dict[str, str | None]
    epochs: tuple[DerivedEpoch, ...]
    total_order: tuple[str, ...]
    rank_of: dict[str, int]  # memory_id -> 0-based index in total_order
    epoch_of: dict[str, int]  # memory_id -> epoch index


@dataclass(frozen=True)
class FirstArrivedSuggestion:
    winner_memory_id: str
    loser_memory_id: str
    recommended_resolution: str  # "b_superseded_by_a" | "a_superseded_by_b"
    winner_rank: int
    loser_rank: int
    winner_epoch: int
    loser_epoch: int
    winner_pivot_memory_id: str | None
    loser_pivot_memory_id: str | None
    explanation: str


def build_parent_tree(
    nodes: Iterable[str], parent_edges: Sequence[tuple[str, str]]
) -> tuple[dict[str, str | None], dict[str, list[str]]]:
    """Return ``(parent_of, children_of)`` over ``nodes`` (cycle-safe, deterministic)."""
    node_set = set(nodes)
    parent_of: dict[str, str | None] = dict.fromkeys(sorted(node_set), None)
    children_of: dict[str, list[str]] = {n: [] for n in parent_of}
    for frm, to in parent_edges:
        if frm not in node_set or to not in node_set or frm == to:
            continue
        if parent_of[to] is not None:
            continue  # first edge wins if legacy DB has >1 parent
        # Guard against a corrupt cycle in parent edges.
        cur: str | None = frm
        seen = {to}
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = parent_of.get(cur)
        if cur is not None:
            continue
        parent_of[to] = frm
        children_of[frm].append(to)
    for ch in children_of.values():
        ch.sort()
    return parent_of, children_of


def compute_subtree_weights(
    parent_of: Mapping[str, str | None],
    children_of: Mapping[str, Sequence[str]],
    node_weights: Mapping[str, float],
) -> dict[str, float]:
    """Bottom-up subtree weight in the parent forest."""
    out: dict[str, float] = {}
    roots = [n for n, p in sorted(parent_of.items()) if p is None]
    order: list[str] = []
    stack = list(roots)
    while stack:
        n = stack.pop()
        order.append(n)
        stack.extend(children_of.get(n, ()))
    for n in reversed(order):
        w = max(1e-6, float(node_weights.get(n, 0.0)))
        for c in children_of.get(n, ()):
            w += out[c]
        out[n] = round(w, 6)
    # Any node somehow not reached from a root (impossible when build_parent_tree is used).
    for n in parent_of:
        if n not in out:
            out[n] = round(max(1e-6, float(node_weights.get(n, 0.0))), 6)
    return out


def select_pivot_chain(
    parent_of: Mapping[str, str | None],
    children_of: Mapping[str, Sequence[str]],
    subtree_weights: Mapping[str, float],
) -> tuple[str, ...]:
    """GHOST / Conflux greedy heaviest-subtree chain from root to leaf."""
    roots = [n for n, p in parent_of.items() if p is None]
    if not roots:
        return ()
    cur = min(roots, key=lambda n: (-subtree_weights.get(n, 0.0), n))
    chain = [cur]
    while True:
        ch = children_of.get(cur, ())
        if not ch:
            break
        cur = min(ch, key=lambda n: (-subtree_weights.get(n, 0.0), n))
        chain.append(cur)
    return tuple(chain)


def _past(pivot: str, rev_adj: Mapping[str, Sequence[str]]) -> set[str]:
    """All nodes ``u`` with a directed path ``u ->* pivot`` (including ``pivot``)."""
    seen = {pivot}
    q = deque([pivot])
    while q:
        cur = q.popleft()
        for prev in rev_adj.get(cur, ()):
            if prev not in seen:
                seen.add(prev)
                q.append(prev)
    return seen


def compute_conflux_consensus(
    nodes: Iterable[str],
    node_weights: Mapping[str, float],
    edges: Sequence[tuple[str, str, str]],
) -> ConfluxConsensus:
    """Compute the Conflux pivot chain, derived epochs and total order.

    ``edges`` is a sequence of ``(from_memory_id, to_memory_id, edge_type)`` over active
    structural edges. Direction ``from -> to`` means ``from`` happened before ``to``.
    """
    node_list = sorted(set(nodes))
    node_set = set(node_list)
    weights = {n: round(float(node_weights.get(n, 0.0)), 6) for n in node_list}
    parent_edges = [
        (a, b) for a, b, t in edges if t == "PARENT" and a in node_set and b in node_set
    ]
    dag_edges = [(a, b) for a, b, _t in edges if a in node_set and b in node_set and a != b]

    parent_of, children_of = build_parent_tree(node_list, parent_edges)
    sub_w = compute_subtree_weights(parent_of, children_of, weights)
    pivot_chain = select_pivot_chain(parent_of, children_of, sub_w)

    rev_adj: dict[str, list[str]] = defaultdict(list)
    for a, b in dag_edges:
        rev_adj[b].append(a)

    assigned: set[str] = set()
    epochs: list[DerivedEpoch] = []
    total: list[str] = []

    for idx, p in enumerate(pivot_chain):
        delta = _past(p, rev_adj) - assigned
        assigned |= delta
        sub_edges = [(a, b) for a, b in dag_edges if a in delta and b in delta]
        ordered = tuple(topological_order(delta, sub_edges))
        epochs.append(DerivedEpoch(index=idx, pivot_memory_id=p, memory_ids=ordered))
        total.extend(ordered)

    unanchored = node_set - assigned
    if unanchored:
        sub_edges = [(a, b) for a, b in dag_edges if a in unanchored and b in unanchored]
        ordered = tuple(topological_order(unanchored, sub_edges))
        epochs.append(
            DerivedEpoch(index=len(pivot_chain), pivot_memory_id=None, memory_ids=ordered)
        )
        total.extend(ordered)

    rank_of = {mid: i for i, mid in enumerate(total)}
    epoch_of = {mid: ep.index for ep in epochs for mid in ep.memory_ids}
    return ConfluxConsensus(
        pivot_chain=pivot_chain,
        subtree_weights=sub_w,
        node_weights=weights,
        parent_of=parent_of,
        epochs=tuple(epochs),
        total_order=tuple(total),
        rank_of=rank_of,
        epoch_of=epoch_of,
    )


def suggest_conflict_winner(
    memory_a_id: str, memory_b_id: str, consensus: ConfluxConsensus
) -> FirstArrivedSuggestion | None:
    """Conflux 'first in total order wins' suggestion for a conflict between ``a`` and ``b``."""
    if memory_a_id not in consensus.rank_of or memory_b_id not in consensus.rank_of:
        return None
    ra, rb = consensus.rank_of[memory_a_id], consensus.rank_of[memory_b_id]
    if ra <= rb:
        win, lose, rw, rl = memory_a_id, memory_b_id, ra, rb
        rec = "b_superseded_by_a"
    else:
        win, lose, rw, rl = memory_b_id, memory_a_id, rb, ra
        rec = "a_superseded_by_b"
    ew, el = consensus.epoch_of[win], consensus.epoch_of[lose]
    pw = consensus.epochs[ew].pivot_memory_id
    pl = consensus.epochs[el].pivot_memory_id
    why = (
        f"Conflux total-order suggestion (first-arrived wins, user confirmation required): "
        f"{win} is at rank #{rw + 1} (epoch {ew}, pivot={pw or 'unanchored'}) before "
        f"{lose} at rank #{rl + 1} (epoch {el}, pivot={pl or 'unanchored'})."
    )
    return FirstArrivedSuggestion(
        winner_memory_id=win,
        loser_memory_id=lose,
        recommended_resolution=rec,
        winner_rank=rw,
        loser_rank=rl,
        winner_epoch=ew,
        loser_epoch=el,
        winner_pivot_memory_id=pw,
        loser_pivot_memory_id=pl,
        explanation=why,
    )

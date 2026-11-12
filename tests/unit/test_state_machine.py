import itertools

import pytest

from ame.domain.enums import ActorType, MemoryStatus
from ame.domain.state_machine import RULES, authorize_transition, initial_status
from ame.errors import InvalidTransitionError, TransitionNotPermittedError

ALLOWED = {(r.from_status, r.to_status) for r in RULES}
ALL_PAIRS = list(itertools.product(MemoryStatus, MemoryStatus))


def test_rule_table_size():
    # T3..T8 (6) + T9 x5 + T10 x5 + T11 + T12 = 18
    assert len(ALLOWED) == len(RULES) == 18


@pytest.mark.parametrize(("frm", "to"), [p for p in ALL_PAIRS if p not in ALLOWED])
def test_every_unlisted_pair_is_illegal(frm, to):
    for actor in ActorType:
        with pytest.raises(InvalidTransitionError):
            authorize_transition(frm, to, actor)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: f"{r.code}-{r.from_status}-{r.to_status}")
def test_actor_permissions(rule):
    for actor in ActorType:
        if actor in rule.actors:
            assert authorize_transition(rule.from_status, rule.to_status, actor) is rule
        else:
            with pytest.raises(TransitionNotPermittedError):
                authorize_transition(rule.from_status, rule.to_status, actor)


def test_initial_status():
    assert initial_status(ActorType.USER) == (MemoryStatus.ACTIVE, "T2")
    assert initial_status(ActorType.AGENT) == (MemoryStatus.CANDIDATE, "T1")
    assert initial_status(ActorType.SYSTEM) == (MemoryStatus.CANDIDATE, "T1")


def test_only_user_reviews():
    for frm, to in [("candidate", "active"), ("candidate", "rejected")]:
        with pytest.raises(TransitionNotPermittedError):
            authorize_transition(MemoryStatus(frm), MemoryStatus(to), ActorType.AGENT)

import itertools

import pytest

from ame.application.schemas import ClassificationChange
from ame.domain.enums import Layer, MemoryType
from ame.errors import ReasonRequiredError, ValidationFailedError
from tests.conftest import AGENT, USER, make


@pytest.mark.parametrize(("layer", "mtype"), list(itertools.product(Layer, MemoryType)))
def test_all_layer_type_combinations(svc, layer, mtype):
    m = svc.create_memory(make(layer=layer, memory_type=mtype), AGENT)
    assert (m.layer, m.memory_type) == (layer, mtype)


def test_promotion_changes_layer_keeps_type(svc):
    m = svc.create_memory(make(layer="L2", memory_type="episodic"), AGENT)
    out = svc.change_classification(m.memory_id, ClassificationChange(layer="L3"), USER)
    assert (out.layer, out.memory_type) == ("L3", "episodic")
    ev = svc.list_audit(m.memory_id)[-1]
    assert ev.event_type == "MEMORY_UPDATED"
    assert (ev.before, ev.after) == ({"layer": "L2"}, {"layer": "L3"})


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_type_change_without_reason_rejected(svc, reason):
    m = svc.create_memory(make(memory_type="episodic"), AGENT)
    with pytest.raises(ReasonRequiredError):
        svc.change_classification(
            m.memory_id, ClassificationChange(memory_type="semantic", reason=reason), USER
        )
    assert svc.get_memory(m.memory_id).memory_type == "episodic"


def test_type_change_with_reason_audited(svc):
    m = svc.create_memory(make(memory_type="episodic"), AGENT)
    out = svc.change_classification(
        m.memory_id, ClassificationChange(memory_type="semantic", reason="generalized"), USER
    )
    assert out.memory_type == "semantic"
    assert svc.list_audit(m.memory_id)[-1].reason == "generalized"


def test_noop_classification_rejected(svc):
    m = svc.create_memory(make(), AGENT)
    with pytest.raises(ValidationFailedError):
        svc.change_classification(m.memory_id, ClassificationChange(layer=m.layer), USER)

"""Saved display details reuse the certified cycle; old records remain readable."""

import pytest

from oilfield_energy.workflows.single_execution.contracts import UnifiedExecution
from oilfield_energy.workflows.single_execution.api import (
    UnifiedSingleController,
    verify_unified_execution,
)
from tests.workflows.test_unified_single import domain, initial, observation


@pytest.fixture(scope="module")
def evidence():
    d = domain()
    controller = UnifiedSingleController(d, initial())
    row = controller.advance(
        observation(d, 0), context_id=d.context_id, target=(0, 0), proposal=None
    )
    return UnifiedExecution(
        domain=d,
        initial=initial(),
        status="unproven",
        requested_steps=2,
        tracking_status="not_assessed",
        steps=(row,),
        plans=tuple(controller.plans),
        terminal_proof_digest=controller.terminal_proof_digest,
    )


def test_display_cycle_is_the_actual_certified_transition(evidence):
    row = evidence.steps[0]
    cycle = row.cycle_evidence
    assert cycle is not None
    assert cycle.command == row.command
    assert cycle.predicted_endpoint == row.after
    assert cycle.endpoint_network.pcc_p_mw == row.pcc_p_mw
    assert cycle.endpoint_network.pcc_q_mvar == row.pcc_q_mvar
    assert cycle.path_network.certified
    assert len(cycle.path_network.voltage_bounds) == 2
    assert len(cycle.path_network.branch_mva_upper) == 1
    verify_unified_execution(evidence)


def test_missing_historical_details_are_not_fabricated(evidence):
    raw = evidence.model_dump(mode="json")
    del raw["steps"][0]["cycle_evidence"]
    old = UnifiedExecution.model_validate(raw)
    assert old.steps[0].cycle_evidence is None
    verify_unified_execution(old)


def test_live_inputs_are_the_committed_observations_not_future_forecasts():
    from oilfield_energy.workflows.single_execution.api import run_unified_feedback
    events = []
    d = domain()

    def unavailable(*args):
        raise RuntimeError("no economic proposal")

    execution = run_unified_feedback(
        d, initial(), total_steps=1, update_steps=15, initial_target=(0, 0),
        observe=lambda k: observation(d, k), propose=unavailable, live=events.append,
    )
    assert len(execution.steps) == 1, execution.reason
    cycles = [e for e in events if e["kind"] == "cycle"]
    assert len(cycles) == 1
    assert cycles[0]["minute"] == execution.steps[0].step + 1
    assert cycles[0]["row"]["observed"] == execution.steps[0].observed.model_dump(mode="json")
    assert cycles[0]["row"]["observed"] != d.forecasts[0].model_dump(mode="json")
    assert "cycle_evidence" not in cycles[0]["row"]


@pytest.mark.parametrize("change", ["endpoint", "network_axis", "network_bound"])
def test_modified_display_details_cannot_survive_validation_or_replay(evidence, change):
    raw = evidence.model_dump(mode="json")
    cycle = raw["steps"][0]["cycle_evidence"]
    if change == "endpoint":
        cycle["predicted_endpoint"]["energy_mwh"] += 0.1
    elif change == "network_axis":
        cycle["path_network"]["branch_mva_upper"] = []
    else:
        cycle["path_network"]["voltage_bounds"][1]["low"] -= 0.00001
    with pytest.raises(ValueError):
        verify_unified_execution(UnifiedExecution.model_validate(raw))

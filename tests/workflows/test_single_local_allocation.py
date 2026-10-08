"""Same-state, same-target counterfactuals and fixed-target admission priority."""

import json
from pathlib import Path

import pytest

from oilfield_energy.modules.control.api import (
    approach_device_command,
    certify_device_transition,
    point_box,
)
from oilfield_energy.modules.control.contracts import TubeCommand, TubeState
from oilfield_energy.modules.dispatch.contracts import CostRates
from oilfield_energy.workflows.single_execution.api import admit_tracking_target
from oilfield_energy.workflows.single_execution.certificates import (
    certify_unified_plan,
    certify_unified_terminal,
    device_model_at,
    unified_network_box,
)
from oilfield_energy.workflows.single_execution.contracts import (
    SingleForecastEnvelope,
    UnifiedDomain,
)
from tests.workflows.test_unified_single import tracking_domain


@pytest.mark.parametrize("index", (0, 1))
def test_historical_state_local_repair_preserves_target_and_complete_continuation(
    index,
):
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/single_execution/local_projection_counterexamples.json"
        ).read_text(encoding="utf-8")
    )
    case = fixture["cases"][index]
    # Project archived physical inputs into the current algorithm for a new
    # counterfactual; never relabel the archived execution as current evidence.
    domain = UnifiedDomain.model_validate({**case["domain"], "version": "unified-single-v7"})
    state = TubeState.model_validate(case["before"])
    proposal = TubeCommand.model_validate(case["proposal"])
    model = device_model_at(domain, 0)
    repaired = approach_device_command(
        model, state, proposal, next_model=device_model_at(domain, 1)
    )
    plan = certify_unified_plan(domain, point_box(state), 0, (repaired,))
    assert certify_unified_terminal(domain)
    end = certify_device_transition(model, point_box(state), repaired).endpoint
    ac = unified_network_box(
        domain, end, SingleForecastEnvelope.model_validate(case["observed"])
    )
    assert plan.commands[0] == repaired and not case["original_tracking_passed"]
    assert ac.certified and ac.pcc_p_mw and ac.pcc_q_mvar
    for bounds, target, tolerance in zip(
        (ac.pcc_p_mw, ac.pcc_q_mvar),
        case["target"],
        (domain.p_band_mw, domain.q_band_mvar),
        strict=True,
    ):
        assert max(abs(bounds.low - target), abs(bounds.high - target)) <= tolerance
    changed = [
        i
        for i, (a, b) in enumerate(zip(proposal.p_mw, repaired.p_mw, strict=True))
        if a != b
    ]
    assert len(changed) == 1
    assert repaired.q_mvar == proposal.q_mvar


def test_certified_original_target_wins_before_cheaper_target_search():
    d = tracking_domain().model_copy(
        update={
            "economic_rates": CostRates(
                curtailment_cny_per_mwh=100,
                storage_throughput_cny_per_mwh=10,
                loss_cny_per_mwh=20,
            ),
            "economic_prices_cny_per_mwh": (400,) * 30,
        }
    )
    state = TubeState(p_mw=(0, 0, 0, 0), q_mvar=(0, 0, 0, 0.2), energy_mwh=1)
    nominal = TubeCommand(p_mw=(1, 0.4, 0, 0), q_mvar=(0, 0, 0, 0.2))
    a = admit_tracking_target(
        d, state, 0, 15, (1.6, 0.1), nominal, "economic", None, 10
    )
    assert a.accepted and a.attempted_targets == ((1.6, 0.1),)
    assert a.plan and a.plan.tracking
    t = a.plan.tracking
    assert t.deadline_step == 10 and (t.target_p_mw, t.target_q_mvar) == (1.6, 0.1)
    certified = [x for x in t.allocation_trials if x.status == "certified"]
    assert len(certified) > 1
    selected = min(
        certified,
        key=lambda x: (x.intervention, x.economic_cost_cny or 0, x.construction),
    )
    assert t.construction == selected.construction

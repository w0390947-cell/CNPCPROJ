"""Replacement proofs must preserve every unexpired PCC tracking obligation."""

import pytest

from oilfield_energy.modules.control.api import point_box
from oilfield_energy.modules.control.contracts import TubeCommand, TubeState
from oilfield_energy.modules.power_flow.contracts import PowerInterval as I
from oilfield_energy.workflows.single_execution.api import (
    UnifiedSingleController,
    certify_tracking_target,
)
from tests.workflows.test_unified_single import tracking_domain


TARGET = (1.6, 0.1)


def observed(domain, step):
    forecast = domain.forecasts[step]
    p, q = (2.99, 0.31) if step % 2 else (3.01, 0.29)
    return forecast.model_copy(
        update={
            "loads": (
                forecast.loads[0],
                forecast.loads[1].model_copy(
                    update={"p_mw": I(low=p, high=p), "q_mvar": I(low=q, high=q)}
                ),
            )
        }
    )


def controller_at(completed):
    domain = tracking_domain()
    state = TubeState(p_mw=(0, 0, 0, 0), q_mvar=(0, 0, 0, 0.2), energy_mwh=1)
    nominal = TubeCommand(p_mw=(1, 0.4, 0, 0), q_mvar=(0, 0, 0, 0.2))
    controller = UnifiedSingleController(domain, state)
    plan = certify_tracking_target(domain, point_box(state), 0, 15, 10, TARGET, nominal)
    for step in range(completed):
        controller.advance(
            observed(domain, step),
            context_id=domain.context_id,
            target=TARGET,
            proposal=None,
            target_plan=plan if step == 0 else None,
            target_changed=step == 0,
        )
    return domain, controller, nominal, plan


@pytest.mark.parametrize("completed,end", [(1, 10), (11, 12)])
@pytest.mark.parametrize("retained", [False, True])
def test_shorter_guarantee_is_rejected_without_mutation_and_old_plan_still_tracks(
    completed, end, retained
):
    domain, controller, nominal, old_plan = controller_at(completed)
    shortened = certify_tracking_target(
        domain, point_box(controller.state), completed, end, 10, TARGET, nominal
    )
    before = {
        key: tuple(value) if isinstance(value, list) else value
        for key, value in vars(controller).items()
    }
    with pytest.raises(ValueError, match="shortens an active tracking guarantee"):
        controller.advance(
            observed(domain, completed),
            context_id=domain.context_id,
            target=TARGET,
            proposal=None,
            target_plan=shortened,
            target_retained=retained,
        )
    assert {
        key: tuple(value) if isinstance(value, list) else value
        for key, value in vars(controller).items()
    } == before

    # Retry the same cycle with its retained proof, through the original expiry.
    for step in range(completed, 15):
        row = controller.advance(
            observed(domain, step),
            context_id=domain.context_id,
            target=TARGET,
            proposal=None,
        )
        assert controller.plans[-1] == old_plan
        assert row.plan_offset == step
        assert row.deadline_step == 10
        if step + 1 >= 10:
            assert row.tracking_certified


@pytest.mark.parametrize("end", [15, 20])
@pytest.mark.parametrize("retained", [False, True])
def test_equal_or_longer_guarantee_preserves_tracking_and_original_deadline(
    end, retained
):
    domain, controller, nominal, _ = controller_at(11)
    replacement = certify_tracking_target(
        domain, point_box(controller.state), 11, end, 10, TARGET, nominal
    )
    for step in range(11, end):
        row = controller.advance(
            observed(domain, step),
            context_id=domain.context_id,
            target=TARGET,
            proposal=None,
            target_plan=replacement if step == 11 else None,
            target_retained=retained if step == 11 else False,
        )
        assert controller.plans[-1] == replacement
        assert row.plan_offset == step - 11
        assert row.deadline_step == 10
        assert row.tracking_certified


@pytest.mark.parametrize("target,deadline", [(TARGET, 10), ((1.7, 0.1), 25)])
def test_update_at_original_expiry_accepts_same_or_new_target(target, deadline):
    domain, controller, nominal, _ = controller_at(15)
    replacement = certify_tracking_target(
        domain, point_box(controller.state), 15, 30, deadline, target, nominal
    )
    for step in range(15, 30):
        row = controller.advance(
            observed(domain, step),
            context_id=domain.context_id,
            target=target,
            proposal=None,
            target_changed=step == 15 and target != TARGET,
            target_plan=replacement if step == 15 else None,
        )
        assert controller.plans[-1] == replacement
        assert row.deadline_step == deadline
        if step + 1 >= deadline:
            assert row.tracking_certified

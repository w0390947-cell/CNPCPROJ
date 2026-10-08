"""Independent ordinary-Q transition examples and capability conflict witnesses."""

from math import exp

import pytest

from oilfield_energy.modules.control.api import assess_reactive_cycle, reactive_cycle_response


def evidence(q0, targets, actual, lower, upper, *, alpha=1.0, ramp=0.9, applied=True):
    return assess_reactive_cycle(
        resource_ids=tuple(f"resource-{i}" for i in range(len(q0))),
        initial_mvar=q0,
        targets_mvar=targets,
        actual_mvar=actual,
        minimum_mvar=lower,
        maximum_mvar=upper,
        alpha=alpha,
        ramp_mvar=ramp,
        response_applied=applied,
    )


def test_original_counterexample_counts_from_real_start():
    alpha = 1 - exp(-0.5)
    actual = reactive_cycle_response((1.5,), (-1.305,), alpha=alpha, ramp_mvar=0.9)
    assert actual == pytest.approx((0.6,), abs=1e-12)
    result = evidence((1.5,), (-1.305,), actual, (-1.305,), (1.305,), alpha=alpha)
    assert result.aggregate_movement_mvar == pytest.approx(0.9, abs=1e-12)
    assert result.minimum_recovery_movement_mvar == pytest.approx(0.195, abs=1e-12)
    assert not result.initial_capacity_within_limits
    assert result.actual_capacity_within_limits and result.ramp_compliant
    assert result.capability_reachable_without_arbitration and result.response_matches_prediction
    old = evidence((1.5,), (-1.305,), (0.405,), (-1.305,), (1.305,), alpha=alpha)
    assert old.aggregate_movement_mvar == pytest.approx(1.095, abs=1e-12)
    assert not old.ramp_compliant and not old.response_matches_prediction


def test_opposite_movements_share_absolute_budget_and_preserve_input():
    initial, targets = (1.5, -1.5, 0.0), (-1.0, 1.0, 0.0)
    actual = reactive_cycle_response(initial, targets, alpha=0.5, ramp_mvar=0.9)
    assert actual == pytest.approx((1.05, -1.05, 0.0), abs=1e-12)
    assert sum(actual) == sum(initial) == 0.0
    result = evidence(initial, targets, actual, (-1.1, -1.1, 0.0), (1.1, 1.1, 0.0), alpha=0.5)
    assert result.ramp_compliant and result.actual_capacity_within_limits
    assert result.aggregate_movement_mvar == pytest.approx(0.9, abs=1e-12)
    assert initial == (1.5, -1.5, 0.0)


@pytest.mark.parametrize(
    "alpha,ramp,reachable",
    [(1.0, 0.195, True), (1.0, 0.19, False), (0.05, 10.0, False), (0.4, 0.0, False)],
)
def test_capacity_ramp_and_lag_intersection(alpha, ramp, reachable):
    actual = reactive_cycle_response((1.5,), (-1.305,), alpha=alpha, ramp_mvar=ramp)
    result = evidence((1.5,), (-1.305,), actual, (-1.305,), (1.305,), alpha=alpha, ramp=ramp)
    assert result.capability_reachable_without_arbitration is reachable
    assert result.ramp_compliant and result.response_matches_prediction
    if not reachable:
        assert not result.actual_capacity_within_limits


def test_joint_budget_can_be_insufficient_even_when_each_device_can_recover():
    result = evidence((1.5, -1.5), (1.0, -1.0), (1.05, -1.05), (-1.0, -1.0), (1.0, 1.0))
    assert result.minimum_recovery_movement_mvar == 1.0
    assert not result.capability_reachable_without_arbitration
    assert not result.actual_capacity_within_limits
    assert result.ramp_compliant


def test_fixed_zero_command_and_held_rejection_are_not_instantaneous_clipping():
    actual = reactive_cycle_response((1.0,), (0.0,), alpha=0.4, ramp_mvar=0.9)
    assert actual == pytest.approx((0.6,), abs=1e-12)
    result = evidence((1.0,), (0.0,), actual, (0.0,), (0.0,), alpha=0.4)
    assert not result.capability_reachable_without_arbitration
    assert not result.actual_capacity_within_limits
    held = evidence((1.0,), (0.0,), (1.0,), (0.0,), (0.0,), alpha=0.4, applied=False)
    assert held.aggregate_movement_mvar == 0.0 and held.response_matches_prediction
    assert not held.actual_capacity_within_limits


@pytest.mark.parametrize("alpha", [0.1, 0.4, 1.0])
@pytest.mark.parametrize("ramp", [0.0, 0.2, 1.0, 10.0])
def test_reachability_witness_and_permutation_invariance(alpha, ramp):
    # The nearest-box endpoint needs movements (-.1, +.2, 0). Its inverse-lag
    # command is independently constructed; no solver or implementation helper.
    initial, lower, upper = (1.1, -1.2, 0.3), (-2.0, -1.0, -1.0), (1.0, 2.0, 1.0)
    closest = (1.0, -1.0, 0.3)
    commands = tuple(q + (end - q) / alpha for q, end in zip(initial, closest))
    result = evidence(
        initial, initial, initial, lower, upper, alpha=alpha, ramp=ramp, applied=False
    )
    if result.capability_reachable_without_arbitration:
        assert all(lo <= c <= hi for c, lo, hi in zip(commands, lower, upper))
        response = reactive_cycle_response(initial, commands, alpha=alpha, ramp_mvar=ramp)
        assert response == pytest.approx(closest, abs=1e-12)
    reverse = evidence(
        initial[::-1],
        initial[::-1],
        initial[::-1],
        lower[::-1],
        upper[::-1],
        alpha=alpha,
        ramp=ramp,
        applied=False,
    )
    assert (
        reverse.capability_reachable_without_arbitration
        == result.capability_reachable_without_arbitration
    )


@pytest.mark.parametrize(
    "alpha,ramp", [(0.0, 0.9), (1.1, 0.9), (float("nan"), 0.9), (0.4, -0.1), (0.4, float("inf"))]
)
def test_invalid_dynamics_are_rejected(alpha, ramp):
    with pytest.raises(ValueError):
        reactive_cycle_response((1.0,), (0.0,), alpha=alpha, ramp_mvar=ramp)

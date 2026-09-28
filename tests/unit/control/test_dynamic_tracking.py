"""Analytic step responses and adversarial observation histories."""

from math import exp

import pytest

from oilfield_energy.modules.control.api import assess_dynamic_tracking
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy


def evaluate(actual, target=None, **kwargs):
    return assess_dynamic_tracking(
        time_minutes=tuple(float(i) for i in range(len(actual))),
        target=tuple(target if target is not None else [1.0] * len(actual)),
        actual=tuple(actual),
        step_minutes=1,
        unit="Mvar",
        limit=0.05,
        numerical_tolerance=1e-6,
        policy=kwargs.pop("policy", DynamicTrackingPolicy()),
        **kwargs,
    )


def test_analytic_first_order_response_passes_without_hiding_the_initial_error():
    # Independent closed-form solution for tau=2, unit step; ramp not binding.
    actual = [1 - exp(-(i + 1) / 2) for i in range(15)]
    result = evaluate(actual)
    assert result.status == "passed"
    assert result.raw_max_error == pytest.approx(exp(-0.5))
    assert result.raw_max_error_time_minute == 0
    segment = result.transitions[0]
    assert segment.allowed_response_minutes == 6
    assert segment.entered_band_after_minutes == 6
    assert segment.confirmed_after_minutes == 8
    assert result.post_deadline_max_error == pytest.approx(exp(-3))


def test_nonresponding_device_fails_after_the_deadline():
    result = evaluate([0.0] * 15)
    assert result.response_status == result.steady_status == "violated"
    assert result.longest_outside_band_minutes == 15


def test_late_recovery_does_not_erase_a_deadline_failure():
    result = evaluate([0.0] * 10 + [1.0] * 5)
    assert result.response_status == "violated"
    assert result.steady_status == "violated"


def test_post_settlement_excursion_and_oscillation_still_fail():
    for actual in ([1.0] * 8 + [0.8] + [1.0] * 6, [1, 0.9] * 8):
        result = evaluate(actual)
        assert result.steady_status == "violated"
        assert result.raw_max_error > 0.05


def test_frequent_changes_do_not_reset_continuous_nonresponse():
    result = evaluate([0.0] * 16, [1.0 + i * 0.1 for i in range(16)])
    assert len(result.transitions) == 16
    assert result.response_status == "violated"
    assert result.longest_outside_band_minutes == 16


def test_short_segments_and_early_termination_are_not_certified():
    result = evaluate([1, 1])
    assert result.status == "unknown"
    assert result.transitions[0].post_deadline_samples == 2
    result = evaluate([1.0] * 15, [1.0 + i * 0.0002 for i in range(15)])
    assert result.status == "unknown"


def test_small_reissued_changes_do_not_restart_the_window():
    result = evaluate([1.0] * 15, [1.0 + (i % 2) * 0.00001 for i in range(15)])
    assert result.status == "passed" and len(result.transitions) == 1


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_missing_observations_never_become_a_pass_or_a_physical_zero(missing):
    result = evaluate([1.0] * 5 + [missing] + [1.0] * 9)
    assert result.status == "unknown"
    assert result.invalid_samples == 1 and result.raw_max_error == 0
    result.model_dump_json()


def test_two_steps_each_have_their_own_deadline_and_confirmation():
    actual = [1 - exp(-(i + 1) / 2) for i in range(15)]
    before = actual[-1]
    actual += [2 + (before - 2) * exp(-(i + 1) / 2) for i in range(15)]
    result = evaluate(actual, [1.0] * 15 + [2.0] * 15)
    assert result.status == "passed"
    assert [s.start_minute for s in result.transitions] == [0, 15]
    assert result.transitions[1].allowed_response_minutes == 6


def test_slow_or_zero_ramp_has_a_finite_maximum_deadline():
    for ramp in (0, 0.01):
        policy = DynamicTrackingPolicy(q_ramp_mvar_per_minute=ramp)
        result = evaluate([0.0] * 15, policy=policy)
        assert result.transitions[0].allowed_response_minutes == 10
        assert result.status == "violated"


def test_default_policy_and_custom_limits_are_independent():
    result = assess_dynamic_tracking(
        time_minutes=tuple(range(15)),
        target=(1.0,) * 15,
        actual=(0.97,) * 15,
        step_minutes=1,
        unit="MW",
        limit=0.02,
        numerical_tolerance=1e-6,
        policy=DynamicTrackingPolicy(),
    )
    assert result.steady_status == "violated"


def test_irregular_time_axis_is_rejected_instead_of_assuming_elapsed_time():
    with pytest.raises(ValueError, match="contiguous"):
        assess_dynamic_tracking(
            time_minutes=(0, 1, 3),
            target=(1, 1, 1),
            actual=(1, 1, 1),
            step_minutes=1,
            unit="MW",
            limit=0.05,
            numerical_tolerance=1e-6,
            policy=DynamicTrackingPolicy(),
        )

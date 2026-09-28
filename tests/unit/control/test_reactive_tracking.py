"""Closed-loop response examples with independent lag/ramp/safety prediction."""

from dataclasses import replace

import pytest

from oilfield_energy.modules.control.api import track_reactive_power
from oilfield_energy.modules.control.contracts import ReactiveTrackingPoint


def predictor(*, current=(0.0,), alpha=0.4, ramp=1.0, safe_min=-10.0):
    def evaluate(commands):
        delta = tuple(
            alpha * (command - old) for old, command in zip(current, commands)
        )
        scale = min(1.0, ramp / max(sum(abs(v) for v in delta), 1e-12))
        response = tuple(old + change * scale for old, change in zip(current, delta))
        q = 1.0 - sum(response)
        return ReactiveTrackingPoint(commands, commands, response, q, q >= safe_min)

    return evaluate


@pytest.mark.parametrize("direction", [-1.0, 1.0])
def test_inverts_lag_to_track_both_directions_without_repeated_physical_steps(
    direction,
):
    evaluate = predictor()
    initial = evaluate((0.1 * direction,))
    result = track_reactive_power(
        initial=initial,
        minimum_mvar=(-1.0,),
        maximum_mvar=(1.0,),
        pcc_target_mvar=1 - 0.2 * direction,
        alpha=0.4,
        evaluate=evaluate,
    )
    assert result.point.responses == pytest.approx((0.2 * direction,))
    assert result.point.controls == pytest.approx((0.5 * direction,))
    assert abs(result.final_error_mvar) < 1e-10
    assert initial.controls == (0.1 * direction,)


def test_aggregate_ramp_cannot_be_spent_again_during_feedback_search():
    evaluate = predictor(current=(0.0, 0.0), ramp=0.1)
    result = track_reactive_power(
        initial=evaluate((0.0, 0.0)),
        minimum_mvar=(-1.0, -1.0),
        maximum_mvar=(1.0, 1.0),
        pcc_target_mvar=0.5,
        alpha=0.4,
        evaluate=evaluate,
    )
    assert sum(result.point.responses) == pytest.approx(0.1)
    assert result.final_error_mvar == pytest.approx(0.4)


def test_saturated_or_unavailable_resources_leave_an_honest_residual():
    evaluate = predictor()
    result = track_reactive_power(
        initial=evaluate((1.0,)),
        minimum_mvar=(-1.0,),
        maximum_mvar=(1.0,),
        pcc_target_mvar=0.0,
        alpha=0.4,
        evaluate=evaluate,
    )
    assert result.status == "limited" and result.final_error_mvar == pytest.approx(0.6)
    assert result.evaluations == 0
    blocked = track_reactive_power(
        initial=replace(evaluate((0.0,)), safe=False),
        minimum_mvar=(-1.0,),
        maximum_mvar=(1.0,),
        pcc_target_mvar=0.0,
        alpha=0.4,
        evaluate=evaluate,
    )
    assert blocked.status == "blocked" and blocked.evaluations == 0


def test_unsafe_or_unacknowledged_candidates_cannot_replace_the_safe_baseline():
    evaluate = predictor(safe_min=0.85)
    result = track_reactive_power(
        initial=evaluate((0.0,)),
        minimum_mvar=(-1.0,),
        maximum_mvar=(1.0,),
        pcc_target_mvar=0.8,
        alpha=0.4,
        evaluate=evaluate,
    )
    assert result.point.safe and 0.85 <= result.point.pcc_q_mvar < 1.0
    initial = evaluate((0.0,))
    rejected = track_reactive_power(
        initial=initial,
        minimum_mvar=(-1.0,),
        maximum_mvar=(1.0,),
        pcc_target_mvar=0.8,
        alpha=0.4,
        evaluate=lambda _: replace(initial, pcc_q_mvar=None, safe=False),
    )
    assert rejected.point == initial

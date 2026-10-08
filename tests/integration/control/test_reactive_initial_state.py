"""A3: real AC predictions from outside capability, including bounded failure."""

from dataclasses import replace
from math import exp

import numpy as np
import pytest

from oilfield_energy.data import Line
from oilfield_energy.network_model import (
    assess_network_model,
    build_legacy_network_model_v2,
)
from oilfield_energy.network_scenarios import NetworkSecurityLimits
from oilfield_energy.reactive_correction import correct_reactive_dispatch
from oilfield_energy.reactive_execution import ReactiveCapability


def correction(
    *,
    initial=(1.5,),
    lower=(-1.305,),
    upper=(1.305,),
    target=(-1.305,),
    alpha=1 - exp(-0.5),
    ramp=0.9,
    project=None,
    limits=None,
):
    model = build_legacy_network_model_v2(
        network_id="a3-q-state",
        buses=["A", "B"],
        pcc_bus_id="A",
        base_mva=10.0,
        lines=[Line("AB", "A", "B", 0.001, 0.001, 6.0)],
    )
    network = assess_network_model(model).require_current_solver_ready()
    # Gross Q=0; net demand corresponds to the TRUE initial Q.
    current = np.array(initial)
    result = correct_reactive_dispatch(
        network,
        limits or NetworkSecurityLimits(0.95, 1.05, 0.15, 4.0, 0.9),
        p_demand_mw=np.array([0.0, 3.0]),
        q_demand_before_mvar=np.array([0.0, -sum(initial)]),
        resource_buses=["B"] * len(initial),
        current_q=current,
        preferred_targets=np.array(target),
        capabilities=tuple(
            ReactiveCapability(str(i), lo, hi)
            for i, (lo, hi) in enumerate(zip(lower, upper))
        ),
        project_targets=project or (lambda x: x),
        slack_voltage_pu=1.0,
        alpha=alpha,
        ramp_mvar=ramp,
    )
    np.testing.assert_array_equal(current, initial)
    return result


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_actual_ac_accepts_recovery_using_true_initial_state(sign):
    result = correction(initial=(sign * 1.5,), target=(-sign * 1.305,))
    assert result.status == "safe"
    assert result.predicted_actual == pytest.approx((sign * 0.6,), abs=1e-10)
    assert abs(result.predicted_actual[0] - sign * 1.5) <= 0.9 + 1e-10
    assert result.minimum_constraint_margin >= -1e-8


def test_network_safe_response_outside_device_capacity_cannot_be_certified():
    # With alpha=.05 even commanding -1.305 cannot reach the new upper bound.
    result = correction(alpha=0.05)
    assert result.status in ("best_effort", "held")
    assert result.predicted_actual[0] > 1.305
    assert result.minimum_constraint_margin < 0


def test_zero_budget_and_rejected_projection_keep_actual_outside_state():
    for kwargs in ({"ramp": 0.0}, {"project": lambda _: np.array([1.5])}):
        result = correction(**kwargs)
        assert result.status == "held"
        assert result.predicted_actual == pytest.approx((1.5,), abs=1e-12)
        assert result.minimum_constraint_margin < 0


def test_fixed_zero_command_can_make_partial_progress_without_claiming_safety():
    result = correction(
        initial=(1.0,), lower=(0.0,), upper=(0.0,), target=(0.0,), alpha=0.4
    )
    assert result.status == "best_effort"
    assert result.targets == pytest.approx((0.0,), abs=1e-12)
    assert result.predicted_actual == pytest.approx((0.6,), abs=1e-12)
    assert result.minimum_constraint_margin < 0


def test_failed_network_evidence_does_not_rewrite_outside_initial_state():
    limits = replace(
        NetworkSecurityLimits(0.95, 1.05, 0.15, 4.0, 0.9), max_iterations=1
    )
    result = correction(limits=limits)
    assert result.status == "unavailable"
    assert result.predicted_actual == pytest.approx((1.5,), abs=1e-12)

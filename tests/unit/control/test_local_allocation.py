"""Analytic local projection and independent shared-feedback response checks."""

from itertools import product
from math import isclose

import pytest

from oilfield_energy.kernel.intervals import Interval as I
from oilfield_energy.modules.control.api import (
    approach_device_command,
    balance_policy_command,
    certify_balance_policy,
    certify_device_transition,
    execute_tube_command,
    point_box,
    tube_contains,
)
from oilfield_energy.modules.control.contracts import (
    BalancePolicy,
    TubeCommand,
    TubeState,
)
from tests.unit.control.test_device_tube import model


def test_slow_wind_does_not_scale_storage_or_unrelated_q():
    m = model()
    m = m.model_copy(
        update={
            "resources": (
                m.resources[0].model_copy(update={"ramp_p_mw": 0.05}),
                m.resources[1],
            )
        }
    )
    state = TubeState(p_mw=(1, 0), q_mvar=(0, 0), energy_mwh=1)
    target = TubeCommand(p_mw=(2, 0.4), q_mvar=(0.1, 0.2))
    command = approach_device_command(m, state, target)
    assert command.p_mw == pytest.approx((1.098, 0.4))
    assert command.q_mvar == pytest.approx(target.q_mvar)
    certify_device_transition(m, point_box(state), command)


def test_shared_q_budget_is_least_squares_not_common_scaling():
    m = model().model_copy(update={"aggregate_q_ramp_mvar": 0.1})
    state = TubeState(p_mw=(2, 0), q_mvar=(0, 0), energy_mwh=1)
    command = approach_device_command(
        m, state, TubeCommand(p_mw=(2, 0), q_mvar=(0.4, 0.1))
    )
    # Projection of (0.4,0.1) onto |q1|+|q2|<=0.245 is (0.245,0).
    assert command.q_mvar == pytest.approx((0.245, 0))
    certify_device_transition(m, point_box(state), command)


def test_next_capability_repairs_only_constrained_resource():
    m = model()
    state = TubeState(p_mw=(1, 0), q_mvar=(0, 0), energy_mwh=1)
    next_model = m.model_copy(
        update={
            "resources": (
                m.resources[0].model_copy(update={"p": I(low=0, high=1.05)}),
                m.resources[1],
            )
        }
    )
    target = TubeCommand(p_mw=(1.2, 0.2), q_mvar=(0, 0.1))
    command = approach_device_command(m, state, target, next_model=next_model)
    transition = certify_device_transition(m, point_box(state), command)
    assert transition.endpoint.p_mw[0].high <= 1.05
    assert command.p_mw[1] == target.p_mw[1] and command.q_mvar == target.q_mvar


def test_joint_wind_projection_respects_q_over_p_and_circle():
    m = model().model_copy(update={"alpha_q": 0.5})
    state = TubeState(p_mw=(1, 0), q_mvar=(0.2, 0), energy_mwh=1)
    command = approach_device_command(
        m, state, TubeCommand(p_mw=(0.5, 0), q_mvar=(0.3, 0))
    )
    assert abs(command.q_mvar[0]) <= 0.3 * command.p_mw[0] + 1e-14
    assert command.p_mw[0] > 0.5  # Projection must jointly move P and Q.
    assert command.q_mvar[0] < 0.3


def test_two_regulators_certify_residual_that_neither_can_handle_alone():
    m = model()
    wind = m.resources[0].model_copy(update={"ramp_p_mw": 0.06})
    wind2 = wind.model_copy(update={"resource_id": "wind2"})
    m = m.model_copy(update={"resources": (wind, wind2, m.resources[1])})
    state = TubeState(p_mw=(1, 1, 0), q_mvar=(0, 0, 0), energy_mwh=1)
    base = TubeCommand(p_mw=state.p_mw, q_mvar=state.q_mvar)
    for i in (0, 1):
        with pytest.raises(ValueError, match="response budget"):
            certify_balance_policy(
                m,
                point_box(state),
                BalancePolicy(base=base, p_regulator=i, net_p_mw=1),
                I(low=2.9, high=3.1),
                I(low=0, high=0),
            )
    policy = BalancePolicy(base=base, p_participation=(1, 1, 0), net_p_mw=1)
    proof = certify_balance_policy(
        m, point_box(state), policy, I(low=2.9, high=3.1), I(low=0, high=0)
    )
    # Includes settling discontinuities near zero residual, not just endpoints.
    for residual in (
        -0.1,
        -0.00019,
        -0.0002,
        -0.00021,
        0,
        0.00019,
        0.0002,
        0.00021,
        0.1,
    ):
        command = balance_policy_command(m, state, policy, 3 + residual, 0)
        actual = execute_tube_command(m, state, command)
        assert tube_contains(proof.endpoint, actual)
        assert abs(3 + residual - sum(actual.p_mw) - 1) <= proof.balance_p_error_mw
        assert isclose(command.p_mw[0], command.p_mw[1])


def test_multiaxis_feedback_covers_interior_load_and_state_samples():
    m = model()
    state = TubeState(p_mw=(2, 0), q_mvar=(0.1, 0), energy_mwh=1)
    box = point_box(state).model_copy(
        update={
            "p_mw": (I(low=1.99, high=2.01), I(low=-0.01, high=0.01)),
            "q_mvar": (I(low=0.09, high=0.11), I(low=-0.01, high=0.01)),
        }
    )
    policy = BalancePolicy(
        base=TubeCommand(p_mw=state.p_mw, q_mvar=state.q_mvar),
        p_participation=(1, 2),
        q_participation=(2, 1),
        net_p_mw=1,
        net_q_mvar=0.2,
    )
    proof = certify_balance_policy(
        m, box, policy, I(low=2.98, high=3.02), I(low=0.28, high=0.32)
    )
    for px, qx, load_p, load_q in product(
        (-1, 0, 1), (-1, 0, 1), (2.98, 3, 3.02), (0.28, 0.3, 0.32)
    ):
        initial = state.model_copy(
            update={
                "p_mw": (2 + px * 0.01, -px * 0.01),
                "q_mvar": (0.1 + qx * 0.01, -qx * 0.01),
            }
        )
        command = balance_policy_command(m, initial, policy, load_p, load_q)
        actual = execute_tube_command(m, initial, command)
        assert tube_contains(proof.endpoint, actual)
        assert abs(load_p - sum(actual.p_mw) - 1) <= proof.balance_p_error_mw
        assert abs(load_q - sum(actual.q_mvar) - 0.2) <= proof.balance_q_error_mvar

"""Path capabilities, one shared response budget and zero-order-hold SOC."""

import pytest
from itertools import product

from oilfield_energy.modules.control.api import (
    balance_policy_command,
    certify_balance_policy,
    approach_backup_command,
    certify_device_transition,
    execute_tube_command,
    point_box,
)
from oilfield_energy.modules.control.contracts import (
    BalancePolicy,
    TubeBox,
    TubeCommand,
    TubeDeviceModel,
    TubeResource,
    TubeState,
)
from oilfield_energy.kernel.intervals import Interval as I


def model():
    return TubeDeviceModel(
        resources=(
            TubeResource(
                resource_id="w",
                bus="b",
                kind="wind",
                p=I(low=0, high=3),
                q=I(low=-1, high=1),
                s_mva=4,
                q_over_p=0.3,
                alpha_p=0.5,
                ramp_p_mw=1,
            ),
            TubeResource(
                resource_id="s",
                bus="b",
                kind="storage",
                p=I(low=-1, high=1),
                q=I(low=-1, high=1),
                s_mva=2,
                alpha_p=1,
                ramp_p_mw=0.9,
            ),
        ),
        alpha_q=0.4,
        aggregate_q_ramp_mvar=0.9,
        step_minutes=1,
        energy_mwh=I(low=0.2, high=2),
        eta_charge=0.9,
        eta_discharge=0.8,
        context_id="fixed",
    )


def test_affine_feedback_contains_every_state_and_load_corner_and_integrates_once():
    m = model()
    source = TubeBox(
        p_mw=(I(low=1.99, high=2.01), I(low=-0.01, high=0.01)),
        q_mvar=(I(low=0.09, high=0.11), I(low=-0.01, high=0.01)),
        energy_mwh=I(low=0.99, high=1.01),
    )
    policy = BalancePolicy(
        base=TubeCommand(p_mw=(2, 0), q_mvar=(0.1, 0)),
        p_regulator=1,
        q_regulator=1,
        net_p_mw=1,
        net_q_mvar=0.2,
    )
    proof = certify_balance_policy(
        m, source, policy, I(low=2.9, high=3.1), I(low=0.29, high=0.31)
    )
    for wp, sp, wq, sq, e, load_p, load_q in product(
        (1.99, 2.01),
        (-0.01, 0.01),
        (0.09, 0.11),
        (-0.01, 0.01),
        (0.99, 1.01),
        (2.9, 3.1),
        (0.29, 0.31),
    ):
        state = TubeState(p_mw=(wp, sp), q_mvar=(wq, sq), energy_mwh=e)
        command = balance_policy_command(m, state, policy, load_p, load_q)
        actual = execute_tube_command(m, state, command)
        # Independent affine balance and zero-order-hold energy calculation.
        assert load_p - sum(actual.p_mw) == pytest.approx(1, abs=1e-12)
        assert load_q - sum(actual.q_mvar) == pytest.approx(
            0.2, abs=m.settling_resolution + 1e-12
        )
        u = command.p_mw[1]
        expected_e = e + (0.9 * max(-u, 0) - max(u, 0) / 0.8) / 60
        assert actual.energy_mwh == pytest.approx(expected_e, abs=2e-15)
        assert proof.endpoint.energy_mwh.contains(actual.energy_mwh)
        assert all(b.contains(x) for b, x in zip(proof.endpoint.p_mw, actual.p_mw))
        assert all(b.contains(x) for b, x in zip(proof.endpoint.q_mvar, actual.q_mvar))


def test_backup_does_not_keep_storage_discharging_while_wind_q_recovers():
    m = model().model_copy(update={"alpha_q": 0.5})
    state = TubeState(p_mw=(2, 0.2), q_mvar=(0.58, 0), energy_mwh=0.2)
    target = TubeCommand(p_mw=(0, 0), q_mvar=(0, 0))
    command = approach_backup_command(m, state, target)
    assert command.p_mw[1] == 0
    actual = execute_tube_command(m, state, command)
    assert actual.energy_mwh == 0.2
    assert abs(actual.q_mvar[0]) <= 0.3 * actual.p_mw[0]


def test_zero_q_pv_can_follow_a_falling_ceiling_without_waiting_for_wind():
    m = model()
    wind = m.resources[0].model_copy(update={"ramp_p_mw": 0.02})
    pv = TubeResource(
        resource_id="pv",
        bus="b",
        kind="pv",
        p=I(low=0, high=0.2),
        q=I(low=0, high=0),
        s_mva=0.2,
        alpha_p=0.98,
        ramp_p_mw=0.2,
    )
    m = m.model_copy(update={"alpha_q": 0.5, "resources": (wind, m.resources[1], pv)})
    state = TubeState(p_mw=(2, 0.2, 0.1), q_mvar=(0.58, 0, 0), energy_mwh=0.2)
    command = approach_backup_command(
        m, state, TubeCommand(p_mw=(0, 0, 0), q_mvar=(0, 0, 0))
    )
    step = certify_device_transition(m, point_box(state), command)
    assert command.p_mw[1:] == (0, 0)
    assert step.endpoint.p_mw[2].high < 0.005
    assert step.endpoint.energy_mwh.low == 0.2


def test_capability_box_rejects_p_drop_before_q_can_recover():
    state = TubeState(p_mw=(2, 0), q_mvar=(0.57, 0), energy_mwh=1)
    command = TubeCommand(p_mw=(1.6, 0), q_mvar=(0.4, 0))
    with pytest.raises(ValueError, match="path capability"):
        certify_device_transition(model(), point_box(state), command)
    assert state.q_mvar[0] == 0.57


def test_opposing_q_moves_cannot_cancel_the_cycle_budget():
    state = TubeState(p_mw=(3, 0), q_mvar=(0.8, -0.8), energy_mwh=1)
    command = TubeCommand(p_mw=(3, 0), q_mvar=(-0.8, 0.8))
    with pytest.raises(ValueError, match="aggregate response"):
        certify_device_transition(model(), point_box(state), command)


def test_storage_energy_and_response_have_an_independent_integral_check():
    state = TubeState(p_mw=(2, 0), q_mvar=(0, 0), energy_mwh=1)
    command = TubeCommand(p_mw=(2.2, 0.4), q_mvar=(0.1, 0))
    step = certify_device_transition(model(), point_box(state), command)
    actual = execute_tube_command(model(), state, command)
    assert actual.energy_mwh == pytest.approx(1 - 0.4 / 0.8 / 60)
    assert actual.p_mw == pytest.approx((2.1, 0.4))
    assert actual.q_mvar == pytest.approx((0.04, 0))
    assert step.path.energy_mwh.contains(actual.energy_mwh)
    with pytest.raises(ValueError, match="energy envelope"):
        execute_tube_command(
            model(), state.model_copy(update={"energy_mwh": 0.2}), command
        )


def test_finite_settling_uses_the_same_monotone_map_in_execution_and_enclosure():
    from oilfield_energy.modules.control.contracts import TubeBox

    m = model().model_copy(
        update={"version": "unified-device-v1", "settling_resolution": 0.0001}
    )
    box = TubeBox(
        p_mw=(I(low=0.00019, high=0.00021), I(low=0, high=0)),
        q_mvar=(I(low=0, high=0), I(low=0, high=0)),
        energy_mwh=I(low=1, high=1),
    )
    command = TubeCommand(p_mw=(0, 0), q_mvar=(0, 0))
    proof = certify_device_transition(m, box, command)
    assert proof.endpoint.p_mw[0].low == 0
    assert proof.endpoint.p_mw[0].high >= 0.000105
    for p in [0.00019, 0.0002, 0.00021]:
        actual = execute_tube_command(
            m, TubeState(p_mw=(p, 0), q_mvar=(0, 0), energy_mwh=1), command
        )
        assert proof.endpoint.p_mw[0].contains(actual.p_mw[0])
        assert actual.p_mw[0] == (0 if p <= 0.0002 else p / 2)


def test_matched_pq_curtailment_preserves_convex_capability_without_rectangle_artifact():
    m = model().model_copy(update={"alpha_q": 0.5})
    state = TubeState(p_mw=(2, 0), q_mvar=(0.58, 0), energy_mwh=1)
    command = TubeCommand(p_mw=(1, 0), q_mvar=(0.29, 0))
    certificate = certify_device_transition(m, point_box(state), command)
    # The rectangle contains an impossible combination (P=1.5, Q=.58).
    assert certificate.path.q_mvar[0].high > 0.3 * certificate.path.p_mw[0].low
    # Actual matched responses traverse one convex segment, always admissible.
    for fraction in [0, 0.1, 0.25, 0.5, 1]:
        p, q = 2 - fraction, 0.58 - 0.29 * fraction
        assert abs(q) <= 0.3 * p

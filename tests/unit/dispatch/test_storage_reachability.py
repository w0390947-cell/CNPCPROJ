"""Analytic dynamics, first-substep limits and energy; no optimization SDK."""

from math import exp

import pytest

from oilfield_energy.modules.dispatch.api import (
    assess_storage_reachability,
    storage_reachability_rows,
)
from oilfield_energy.modules.dispatch.contracts import (
    StorageReachabilityInput,
    StorageReachabilityPolicy,
)


def inputs(**changes):
    values = dict(
        policy=StorageReachabilityPolicy(),
        resource_id="battery",
        start_minute=0.0,
        steps=1,
        planning_minutes=1.0,
        execution_minutes=1.0,
        time_constant_minutes=2.0,
        ramp_mw_per_minute=0.65,
        maximum_power_mw=3.0,
        initial_power_mw=2.5,
        initial_energy_mwh=2.0,
        minimum_energy_mwh=0.5,
        maximum_energy_mwh=4.0,
        terminal_energy_mwh=2.03,
        terminal_tolerance_mwh=0.05,
        eta_charge=0.95,
        eta_discharge=0.95,
    )
    values.update(changes)
    return StorageReachabilityInput(**values)


def test_audit_counterexample_is_not_reachable_despite_its_consistent_static_energy():
    spec = inputs()
    power = -2.00029
    result = assess_storage_reachability(
        spec,
        p_mw=(power,),
        energy_mwh=(2.0, 2.0 - power * 0.95 / 60),
    )
    assert result.status == "violated" and result.maximum_power_violation_mw > 3
    # Even the most favorable ordinary discharge violates the terminal band.
    assert 2.0 - (2.5 - 0.65) / 0.95 / 60 == pytest.approx(1.9675438596491228)
    assert 2.0 - (2.5 - 0.65) / 0.95 / 60 < 2.03 - 0.05


@pytest.mark.parametrize("minutes", [1.0, 15.0])
def test_coarse_interval_cannot_spend_future_substep_ramp_budget(minutes):
    spec = inputs(planning_minutes=minutes, initial_power_mw=0.0)
    rows = storage_reachability_rows(spec)
    assert any(row.coefficient * 0.7 > row.upper for row in rows)
    assert all(row.coefficient * 0.6 <= row.upper for row in rows)


def test_slow_response_command_limit_can_bind_before_the_ramp_limit():
    spec = inputs(initial_power_mw=0.0, time_constant_minutes=100.0)
    assert any(row.coefficient * 0.1 > row.upper for row in storage_reachability_rows(spec))


@pytest.mark.parametrize("minutes", [1.0, 15.0])
def test_inverse_witness_replays_each_device_substep_and_signed_energy(minutes):
    powers = (-0.2, 0.2, 0.1)
    energy = [2.0]
    for p in powers:
        energy.append(energy[-1] + minutes / 60 * (-p * 0.95 if p < 0 else -p / 0.95))
    spec = inputs(
        steps=3,
        planning_minutes=minutes,
        initial_power_mw=0.0,
        terminal_energy_mwh=energy[-1],
        terminal_tolerance_mwh=0.0,
    )
    proof = assess_storage_reachability(spec, p_mw=powers, energy_mwh=tuple(energy))
    assert proof.status == "passed"
    actual, actual_energy = 0.0, 2.0
    for t, target in enumerate(powers):
        for substep in range(int(minutes)):
            command = proof.first_command_mw[t] if substep == 0 else target
            delta = (1 - exp(-0.5)) * (command - actual)
            actual += max(-0.65, min(0.65, delta))
            actual_energy += (-actual * 0.95 if actual < 0 else -actual / 0.95) / 60
            assert actual == pytest.approx(target, abs=1e-12)
        assert actual_energy == pytest.approx(energy[t + 1], abs=1e-12)


def test_energy_replay_cannot_use_simultaneous_charge_discharge_or_accumulate_residuals():
    spec = inputs(steps=3, initial_power_mw=0.0, terminal_energy_mwh=2.0)
    proof = assess_storage_reachability(
        spec,
        p_mw=(0.0, 0.0, 0.0),
        energy_mwh=(2.0, 1.9999994, 1.9999988, 1.9999982),
    )
    assert proof.status == "violated"
    assert proof.maximum_energy_violation_mwh > 1e-6


def test_startup_is_explicit_and_does_not_remove_later_transitions():
    spec = inputs(steps=2, initial_power_mw=None)
    rows = storage_reachability_rows(spec)
    assert rows and {row.time_index for row in rows} == {1}
    assert any(row.coefficient * -2 + row.previous_coefficient * 2 > row.upper for row in rows)
    with pytest.raises(ValueError):
        inputs(initial_power_mw=None, start_minute=15.0)


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_power_mw": float("nan")},
        {"initial_power_mw": 4.0},
        {"planning_minutes": 1.5},
        {"time_constant_minutes": 0.0},
        {"time_constant_minutes": 1e20},
        {"execution_minutes": 0.0},
        {"ramp_mw_per_minute": -1.0},
        {"initial_energy_mwh": 5.0},
    ],
)
def test_bad_initial_states_and_unsupported_dynamics_are_rejected(changes):
    with pytest.raises(ValueError):
        inputs(**changes)

"""Absolute obligations checked against the original geometric-delay example."""

import pytest

from oilfield_energy.modules.dispatch.api import (
    observe_reserve_recovery,
    plan_storage_reserve,
    reserve_energy_bounds,
)
from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy


def test_five_minute_replans_repay_original_debt_at_fifteen_minutes():
    energy = 0.52
    for now in (0, 5, 10):
        plan = plan_storage_reserve(
            forecast_scale_mw=(0.0,) * 3,
            step_minutes=5,
            remaining_day_minutes=120,
            initial_energy_mwh=energy,
            minimum_energy_mwh=0.5,
            maximum_energy_mwh=4.5,
            maximum_power_mw=2,
            eta_charge=0.95,
            eta_discharge=0.95,
            policy=StorageReservePolicy(minimum_error_mw=0.25),
            recovery_remaining_minutes=15 - now,
            full_support=True,
        )
        energy = plan.energy_floor_mwh[1]
    assert energy == pytest.approx(0.5 + 0.25 * 0.25 / 0.95)


@pytest.mark.parametrize("frequency", [1, 5, 15])
def test_observation_clock_is_independent_of_replan_frequency(frequency):
    task = None
    for minute in range(0, 16, frequency):
        task = observe_reserve_recovery(
            task,
            region="A",
            direction="up",
            minute=minute,
            required_mw=0.25 + minute / 100,
            recovery_minutes=15,
            adequate=False,
        )
        assert task.deadline_minute == 15
        assert task.first_minute == 0
    assert task.breached and task.status == "violated"
    recovered = observe_reserve_recovery(
        task,
        region="A",
        direction="up",
        minute=16,
        required_mw=0.25,
        recovery_minutes=15,
        adequate=True,
    )
    assert recovered.status == "recovered" and recovered.breached


def test_unknown_does_not_invent_breach_or_recovery():
    task = observe_reserve_recovery(
        None,
        region="A",
        direction="down",
        minute=0,
        required_mw=0.25,
        recovery_minutes=15,
        adequate=None,
    )
    task = observe_reserve_recovery(
        task,
        region="A",
        direction="down",
        minute=16,
        required_mw=0.1,
        recovery_minutes=15,
        adequate=None,
    )
    assert task.status == "unknown" and not task.breached
    assert task.deadline_minute == 15 and task.required_mw == 0.25
    later = observe_reserve_recovery(
        task,
        region="A",
        direction="down",
        minute=17,
        required_mw=0.25,
        recovery_minutes=15,
        adequate=True,
    )
    assert later.status == "recovered" and later.deadline_status == "unknown"


def test_solver_retries_do_not_change_deadline_and_new_episode_needs_actual_recovery():
    args = dict(region="A", direction="up", required_mw=0.25, recovery_minutes=15)
    task = observe_reserve_recovery(None, minute=7, adequate=False, **args)
    for _ in range(4):
        task = observe_reserve_recovery(task, minute=12, adequate=False, **args)
        assert task.deadline_minute == 22
    task = observe_reserve_recovery(task, minute=20, adequate=True, **args)
    assert task.deadline_status == "met"
    new = observe_reserve_recovery(task, minute=21, adequate=False, **args)
    assert new.first_minute == 21 and new.deadline_minute == 36


def test_predicted_deadline_energy_includes_the_response_interval():
    bounds = reserve_energy_bounds(
        initial_energy_mwh=0.52,
        initial_power_mw=0,
        minute_targets_mw=(-0.6,) * 5,
        eta_charge=0.95,
        eta_discharge=0.95,
    )
    assert bounds.low == pytest.approx(0.52 + 4 * 0.6 * 0.95 / 60)
    assert bounds.high == pytest.approx(0.52 + 5 * 0.6 * 0.95 / 60)
    # A linearly increasing charging response pays half the first-minute area.
    continuous = 0.52 + 4.5 * 0.6 * 0.95 / 60
    assert bounds.low < continuous < bounds.high

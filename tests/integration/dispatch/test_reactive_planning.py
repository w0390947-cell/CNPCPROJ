"""Shared reserve/dynamic envelope enforced by all three numerical backends."""

from dataclasses import replace
from math import exp

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.cluster_reactive import with_reactive_planning
from oilfield_energy.bootstrap.adapters.cluster_rolling import window_case
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.model import solve_case
from oilfield_energy.modules.control.contracts import (
    DeviceCheckpoint,
    DynamicTrackingPolicy,
)
from oilfield_energy.modules.dispatch.api import plan_reactive_envelope
from oilfield_energy.modules.dispatch.contracts import (
    ReactivePlanningPolicy,
    ReactivePlanningResource,
)
from oilfield_energy.reactive_correction import reactive_response
from oilfield_energy.regional_control import RegionalConvexController


@pytest.mark.parametrize("backend", ["linear", "misocp", "regional"])
def test_backends_enforce_real_state_transition_reserve_and_authority(backend):
    base = build_synthetic_case(96)
    mg = base.microgrids[0]
    case = window_case(
        replace(base, microgrids=[mg]),
        0,
        4,
        {mg.name: mg.storage.e_initial_mwh},
        {mg.name: mg.storage.e_initial_mwh},
    )
    initial = {r.resource_id: 0.0 for r in mg.resource_identities}
    checkpoint = DeviceCheckpoint(
        minute=0,
        storage_energy_mwh=mg.storage.e_initial_mwh,
        storage_power_mw=0,
        reactive_power_mvar=initial,
    )
    planned = with_reactive_planning(
        case,
        policy=ReactivePlanningPolicy(),
        dynamics=DynamicTrackingPolicy(),
        checkpoints={mg.name: checkpoint},
        storage_enabled=True,
    )
    if backend == "regional":
        controller = RegionalConvexController(
            planned, planned.microgrids[0], ADMMConfig()
        )
        controller.solve(None, fallback=True)
        windp = {b: np.array(v.value) for b, v in controller.p_wind.items()}
        windq = {b: np.array(v.value) for b, v in controller.q_wind.items()}
        svg = np.array(controller.q_svg.value)
        assert controller.problem.is_dpp()
    else:
        solver = solve_case if backend == "linear" else solve_case_misocp
        result = solver(planned, [mg.name], time_limit_seconds=20)
        assert result.success
        schedules = {
            s.resource_id: s for s in result.microgrids[mg.name]["resource_schedules"]
        }
        windp = {
            b: schedules[mg.resource_id("wind", b)].active_power_mw
            for b in mg.wind_capacity_mw
        }
        windq = {
            b: schedules[mg.resource_id("wind", b)].reactive_power_mvar
            for b in mg.wind_capacity_mw
        }
        svg = schedules[mg.resource_id("svg", mg.svg_bus)].reactive_power_mvar
        assert all(
            np.max(np.abs(s.reactive_power_mvar)) < 1e-6
            for s in schedules.values()
            if s.resource_id
            in [
                mg.resource_id("storage", mg.storage.bus),
                *(mg.resource_id("pv", b) for b in mg.pv_capacity_mw),
            ]
        )
    assert np.max(np.abs(svg)) <= 1.8 * 0.9 + 1e-6
    for bus in windp:
        assert np.all(np.abs(windq[bus]) <= 0.9 * 0.3 * windp[bus] + 1e-6)
    # Independent reconstruction of the witness, followed by the REAL execution
    # response. End-point row residuals alone are not the dynamic test oracle.
    buses = tuple(windp)
    previous = np.zeros(len(buses) + 1)
    alpha = 1 - exp(-0.5)
    decay = exp(-2.5)
    for t in range(4):
        endpoint = np.array([*(windq[b][t] for b in buses), svg[t]])
        commands = (endpoint - decay * previous) / (1 - decay)
        limits = np.array(
            [
                *(mg.wind_reactive_capability(b).limit_at(windp[b][t]) for b in buses),
                1.8,
            ]
        )
        assert np.all(np.abs(previous) <= limits + 1e-5)
        assert np.all(np.abs(commands) <= limits + 1e-5)
        for _ in range(5):
            assert np.sum(np.abs(alpha * (commands - previous))) <= 0.9 + 1e-5
            previous = reactive_response(previous, commands, alpha=alpha, ramp_mvar=0.9)
            assert np.all(np.abs(previous) <= limits + 1e-5)
        np.testing.assert_allclose(previous, endpoint, atol=1e-5, rtol=0)
    assert mg.svg_q_max_mvar == 1.8 and not mg.storage_reactive_enabled


@pytest.mark.parametrize("step_minutes", [0.25, 1.0, 2.0])
@pytest.mark.parametrize("tau", [0.1, 2.0, 20.0])
@pytest.mark.parametrize("directions", [(1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)])
def test_constant_command_witness_matches_execution_at_every_substep(
    step_minutes, tau, directions
):
    """Signed changes never cancel when accounting for the shared ramp budget."""
    count = 5
    rate = 0.2
    alpha = 1 - exp(-step_minutes / tau)
    initial = np.array([0.1, -0.1])
    # Each first increment uses at most half the common physical ramp budget.
    commands = initial + np.array(directions) * min(
        0.4, rate * step_minutes / (2 * alpha)
    )
    endpoint = commands + (initial - commands) * exp(-count * step_minutes / tau)
    resources = tuple(
        ReactivePlanningResource(
            str(i), ((0.0, 1.0, 1.0), (0.0, -1.0, 1.0)), float(q), True
        )
        for i, q in enumerate(initial)
    )
    rows = plan_reactive_envelope(
        resources,
        steps=1,
        step_minutes=count * step_minutes,
        time_constant_minutes=tau,
        ramp_mvar_per_minute=rate,
        policy=ReactivePlanningPolicy(transition_minutes=count * step_minutes),
        execution_step_minutes=step_minutes,
    )
    assert all(
        row.q_coefficient * endpoint[int(row.resource_id)] <= row.upper + 1e-12
        for row in rows
    )
    actual = initial.copy()
    for _ in range(count):
        raw = alpha * (commands - actual)
        assert np.sum(np.abs(raw)) <= rate * step_minutes + 1e-12
        following = reactive_response(
            actual, commands, alpha=alpha, ramp_mvar=rate * step_minutes
        )
        np.testing.assert_allclose(following, actual + raw, atol=1e-12, rtol=0)
        assert np.all(np.abs(following) <= 1)
        actual = following
    np.testing.assert_allclose(actual, endpoint, atol=1e-12, rtol=0)


def test_adapter_passes_execution_grid_instead_of_assuming_one_minute():
    case = build_synthetic_case(96)
    mg = case.microgrids[0]
    checkpoint = DeviceCheckpoint(
        minute=0,
        storage_energy_mwh=mg.storage.e_initial_mwh,
        storage_power_mw=0,
        reactive_power_mvar={r.resource_id: 0.0 for r in mg.resource_identities},
    )

    def first_svg_positive_bound(step):
        projected = with_reactive_planning(
            replace(case, microgrids=[mg]),
            policy=ReactivePlanningPolicy(),
            dynamics=DynamicTrackingPolicy(),
            checkpoints={mg.name: checkpoint},
            storage_enabled=True,
            execution_step_minutes=step,
        )
        return min(
            row.upper
            for row in projected.reactive_plans[mg.name]
            if row.resource_id == mg.resource_id("svg", mg.svg_bus)
            and row.time_index == 0
            and row.p_coefficient == 0
            and row.q_coefficient == 1
        )

    assert first_svg_positive_bound(0.5) < first_svg_positive_bound(1.0)

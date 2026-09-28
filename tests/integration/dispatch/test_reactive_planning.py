"""Shared reserve/dynamic envelope enforced by all three numerical backends."""

from dataclasses import replace

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
from oilfield_energy.modules.dispatch.contracts import ReactivePlanningPolicy
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
    # Three authorized resources share 0.9 Mvar/min over the five-minute horizon.
    assert abs(svg[0]) <= 1.5 + 1e-6
    assert mg.svg_q_max_mvar == 1.8 and not mg.storage_reactive_enabled

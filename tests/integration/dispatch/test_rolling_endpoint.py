"""The rolling initial condition and endpoint are independent physical values."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.cluster_rolling import (
    forecast_grid,
    window_case,
)
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.model import solve_case
from oilfield_energy.regional_control import RegionalConvexController


def test_forecast_resampling_preserves_energy_and_does_not_change_inputs():
    case = build_synthetic_case(24)
    original = case.microgrids[0]
    forecast = forecast_grid(case, 15)
    assert len(forecast.time_hours) == 96
    for bus, values in original.load_p_mw.items():
        np.testing.assert_array_equal(forecast.microgrids[0].load_p_mw[bus], np.repeat(values, 4))
        assert np.sum(values) == pytest.approx(np.sum(forecast.microgrids[0].load_p_mw[bus]) * 0.25)
    energies = {mg.name: mg.storage.e_min_mwh + 0.2 for mg in case.microgrids}
    terminals = {mg.name: mg.storage.e_initial_mwh for mg in case.microgrids}
    window = window_case(forecast, 4, 20, energies, terminals)
    for mg, source in zip(window.microgrids, case.microgrids):
        assert mg.storage.e_initial_mwh == energies[mg.name]
        assert mg.storage.terminal_energy_mwh == terminals[mg.name]
        assert source.storage.e_terminal_mwh is None
    np.testing.assert_array_equal(window.time_hours, np.arange(4, 20) / 4)


@pytest.mark.parametrize("backend", ["linear", "misocp", "regional"])
def test_backend_obeys_explicit_endpoint_even_for_a_single_interval(backend):
    original = build_synthetic_case(4)
    mg = original.microgrids[0]
    # A moderate charging request is feasible within one hour and distinctly
    # different from returning to the window's observed initial energy.
    mg = replace(mg, storage=replace(mg.storage, e_initial_mwh=1.5, e_terminal_mwh=1.8))
    case = replace(
        original,
        microgrids=[mg],
        assumptions=replace(original.assumptions, dt_hours=1.0),
    )
    case = window_case(case, 0, 1, {mg.name: 1.5}, {mg.name: 1.8})
    if backend == "regional":
        plan = RegionalConvexController(case, case.microgrids[0], ADMMConfig()).solve(
            None, fallback=True
        )
        energy = plan.storage_energy_mwh
    else:
        solver = solve_case if backend == "linear" else solve_case_misocp
        plan = solver(case, [mg.name], time_limit_seconds=20)
        assert plan.success
        energy = plan.microgrids[mg.name]["storage_energy_mwh"]
    assert energy[0] == pytest.approx(1.5)
    assert abs(energy[-1] - 1.8) <= case.assumptions.terminal_energy_tolerance_mwh + 1e-6

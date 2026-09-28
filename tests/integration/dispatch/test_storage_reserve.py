"""Three numerical backends must respect the same planning-only reserve."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.cluster_reserve import with_storage_reserves
from oilfield_energy.bootstrap.adapters.cluster_rolling import window_case
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.model import solve_case
from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy
from oilfield_energy.regional_control import RegionalConvexController


@pytest.mark.parametrize("backend", ["linear", "misocp", "regional"])
@pytest.mark.parametrize("depleted", [False, True])
def test_reserve_constrains_plans_but_does_not_raise_physical_soc(backend, depleted):
    original = build_synthetic_case(96)
    mg = original.microgrids[0]
    energy = mg.storage.e_min_mwh if depleted else mg.storage.e_initial_mwh
    case = replace(original, microgrids=[mg])
    case = window_case(case, 0, 4, {mg.name: energy}, {mg.name: mg.storage.e_min_mwh})
    reserved = with_storage_reserves(case, StorageReservePolicy(), 1440)
    st = reserved.microgrids[0].storage
    assert st.e_min_mwh == mg.storage.e_min_mwh
    assert st.e_initial_mwh == energy
    reserve = reserved.storage_reserves[mg.name]
    if backend == "regional":
        result = RegionalConvexController(
            reserved, reserved.microgrids[0], ADMMConfig()
        ).solve(None, fallback=True)
        energies = result.storage_energy_mwh
        power = result.storage_discharge_mw - result.storage_charge_mw
    else:
        solver = solve_case if backend == "linear" else solve_case_misocp
        result = solver(reserved, [mg.name], time_limit_seconds=20)
        assert result.success
        row = result.microgrids[mg.name]
        energies = row["storage_energy_mwh"]
        power = row["storage_discharge_mw"] - row["storage_charge_mw"]
    assert energies[0] == pytest.approx(energy)
    assert np.all(energies >= np.array(reserve.energy_floor_mwh) - 1e-6)
    assert np.all(energies <= np.array(reserve.energy_ceiling_mwh) + 1e-6)
    assert np.all(power >= np.array(reserve.minimum_power_mw) - 1e-6)
    assert np.all(power <= np.array(reserve.maximum_power_mw) + 1e-6)

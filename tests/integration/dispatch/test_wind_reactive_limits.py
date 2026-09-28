"""Force each bound to be active, independently of the default simulation data."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.data import Line, MicrogridData, ModelAssumptions, ProjectCase, Storage
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.model import solve_case
from oilfield_energy.reactive_execution import calculate_reactive_capabilities
from oilfield_energy.resource_control_contracts import ResourceSchedule, ResourceType


@pytest.mark.parametrize("solver", [solve_case, solve_case_misocp])
@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize(
    "ratio, absolute, expected",
    [(0.333, 1.64, 0.999), (0.30, 1.64, 0.9), (0.333, 0.4, 0.4), (0.0, 1.64, 0.0)],
)
def test_optimizer_and_execution_honor_independent_ratio_and_absolute_bounds(
    solver, sign, ratio, absolute, expected
):
    zero = np.zeros(1)
    mg = MicrogridData(
        name="TEST",
        buses=["PCC", "GEN"],
        lines=[Line("L", "PCC", "GEN", 0.0, 0.0001, 20.0)],
        pcc_bus="PCC",
        base_mva=20.0,
        maximum_load_mw=6.0,
        p_grid_max_mw=20.0,
        p_grid_min_mw=0.0,
        voltage_min_pu=0.9,
        voltage_max_pu=1.1,
        load_p_mw={"PCC": np.array([6.0]), "GEN": zero},
        load_q_mvar={"PCC": np.array([sign * 2.0]), "GEN": zero},
        wind_available_mw={"GEN": np.array([3.0])},
        wind_capacity_mw={"GEN": 5.0},
        wind_capacity_mva={"GEN": 5.25},
        pv_available_mw={},
        pv_capacity_mw={},
        pv_capacity_mva={},
        storage=Storage("PCC", 0.0, 5.0, 0.5, 2.5, 0.95, 0.95, 1.0),
        svg_bus="PCC",
        svg_q_min_mvar=0.0,
        svg_q_max_mvar=0.0,
        wind_q_abs_over_p_max={"GEN": ratio},
        wind_q_abs_max_mvar={"GEN": absolute},
        storage_reactive_enabled=False,
    )
    case = ProjectCase(
        np.array([0.0]),
        np.array([700.0]),
        [mg],
        replace(ModelAssumptions(), pf_min=0.7, pf_dispatch_target=0.7),
        20.0,
    )
    result = solver(
        case,
        storage_enabled=False,
        cluster_coordination=False,
        pcc_targets={"TEST": {"p_mw": np.array([3.0]), "q_mvar": zero}},
        time_limit_seconds=20,
    )
    assert result.success, result.message
    values = result.microgrids["TEST"]
    assert values["wind_used_mw"][0] == pytest.approx(3.0, abs=1e-6)
    assert values["wind_q_mvar"][0] == pytest.approx(sign * expected, abs=1e-6)
    resource = ResourceSchedule("test-wind", "GEN", ResourceType.WIND, zero, zero)
    capability = calculate_reactive_capabilities(mg, (resource,), {"test-wind": 3.0})[0]
    assert capability.minimum_mvar == pytest.approx(-expected)
    assert capability.maximum_mvar == pytest.approx(expected)

"""Analytic resource envelopes for the real coordination SDK adapter.

Synthetic one-bus systems isolate device capability from network losses.
All errors below are numerical MW/Mvar tolerances, not acceptance settings.
"""

from dataclasses import replace
from math import cos, pi

import cvxpy as cp
import numpy as np
import pytest

from oilfield_energy.data import MicrogridData, ModelAssumptions, ProjectCase, Storage
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.modules.dispatch.contracts import DispatchCapabilities
from oilfield_energy.regional_control import RegionalConvexController


def capability_case() -> ProjectCase:
    grid = MicrogridData(
        name="TEST",
        buses=["PCC"],
        lines=[],
        pcc_bus="PCC",
        base_mva=20,
        maximum_load_mw=10,
        p_grid_max_mw=20,
        p_grid_min_mw=0,
        voltage_min_pu=0.9,
        voltage_max_pu=1.1,
        load_p_mw={"PCC": np.array([10.0])},
        load_q_mvar={"PCC": np.zeros(1)},
        wind_available_mw={"W": np.array([4.0])},
        wind_capacity_mw={"W": 5.0},
        wind_capacity_mva={"W": 5.25},
        pv_available_mw={},
        pv_capacity_mw={},
        pv_capacity_mva={},
        storage=Storage("PCC", 2.0, 5.0, 0.5, 2.5, 0.95, 0.95, 2.0),
        svg_bus="PCC",
        svg_q_min_mvar=0,
        svg_q_max_mvar=0,
        wind_q_abs_over_p_max={"W": 0.30},
        wind_q_abs_max_mvar={"W": 1.64},
        storage_reactive_enabled=False,
    )
    return ProjectCase(
        np.zeros(1),
        np.array([700.0]),
        [grid],
        replace(ModelAssumptions(), dt_hours=1, pf_dispatch_target=0.5),
        20,
    )


def controller(
    case: ProjectCase, storage_enabled: bool = False
) -> RegionalConvexController:
    return RegionalConvexController(
        case,
        case.microgrids[0],
        ADMMConfig(),
        loss_calibration={"p_loss_mw": np.zeros(1), "q_loss_mvar": np.zeros(1)},
        capabilities=DispatchCapabilities(storage_enabled=storage_enabled),
    )


def maximum_support(ctrl, sign, extra=()):
    problem = cp.Problem(
        cp.Maximize(sign * ctrl.q_support[0]),
        [*ctrl.autonomous_problem.constraints, *extra],
    )
    assert problem.is_dcp() and problem.is_qp()
    problem.solve(solver="CLARABEL")
    assert problem.status == cp.OPTIMAL
    return float(sign * ctrl.q_support.value[0])


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize(
    "ratio,absolute,p,expected",
    [
        (0.30, 1.64, 3.0, 0.9),
        (0.333, 1.64, 3.0, 0.999),
        (0.30, 0.4, 3.0, 0.4),
        (0.0, 1.64, 3.0, 0.0),
        (0.30, 1.64, 0.0, 0.0),
    ],
)
def test_wind_uses_planned_p_policy_and_absolute_limit(
    sign, ratio, absolute, p, expected
):
    case = capability_case()
    case = replace(
        case,
        microgrids=[
            replace(
                case.microgrids[0],
                wind_q_abs_over_p_max={"W": ratio},
                wind_q_abs_max_mvar={"W": absolute},
            )
        ],
    )
    before = case.microgrids[0].wind_available_mw["W"].copy()
    ctrl = controller(case)
    assert maximum_support(ctrl, sign, [ctrl.p_wind["W"] == p]) == pytest.approx(
        expected, abs=2e-7
    )
    np.testing.assert_array_equal(case.microgrids[0].wind_available_mw["W"], before)


def test_curtailment_and_pv_output_do_not_create_wind_reactive_headroom():
    case = capability_case()
    case = replace(
        case,
        microgrids=[
            replace(
                case.microgrids[0],
                pv_available_mw={"PV": np.array([4.0])},
                pv_capacity_mw={"PV": 5.0},
                pv_capacity_mva={"PV": 5.25},
                pv_reactive_enabled={"PV": False},
            )
        ],
    )
    ctrl = controller(case)
    # Of 2 MW used, 1 MW is PV: wind must provide only 1 MW and at most .3 Mvar.
    assert maximum_support(
        ctrl, 1, [ctrl.p_renew == 2, ctrl.p_pv["PV"] == 1]
    ) == pytest.approx(0.3, abs=2e-7)
    assert abs(ctrl.q_pv["PV"].value[0]) < 2e-7


@pytest.mark.parametrize("kind", ["wind", "pv"])
@pytest.mark.parametrize("enabled", [False, True])
def test_converter_mva_polygon_and_reactive_authority(kind, enabled):
    case = capability_case()
    mg = case.microgrids[0]
    if kind == "wind":
        mg = replace(
            mg,
            wind_q_abs_over_p_max=None,
            wind_q_abs_max_mvar=None,
            wind_capacity_mva={"W": 2.0},
        )
    else:
        mg = replace(
            mg,
            wind_available_mw={},
            wind_capacity_mw={},
            wind_capacity_mva={},
            wind_q_abs_over_p_max={},
            wind_q_abs_max_mvar={},
            pv_available_mw={"PV": np.array([2.0])},
            pv_capacity_mw={"PV": 2.0},
            pv_capacity_mva={"PV": 2.0},
            pv_reactive_enabled={"PV": enabled},
        )
    # Four facets give the analytic square |P|,|Q| <= S/sqrt(2).
    case = replace(
        case, microgrids=[mg], assumptions=replace(case.assumptions, polygon_sides=4)
    )
    ctrl = controller(case)
    p = ctrl.p_wind["W"] if kind == "wind" else ctrl.p_pv["PV"]
    expected = 2 * cos(pi / 4) if kind == "wind" or enabled else 0
    assert maximum_support(ctrl, 1, [p == 1]) == pytest.approx(expected, abs=2e-7)


@pytest.mark.parametrize(
    "storage_enabled,reactive_enabled", [(False, True), (True, False), (True, True)]
)
def test_storage_requires_both_participation_and_reactive_authority(
    storage_enabled, reactive_enabled
):
    case = capability_case()
    case = replace(
        case,
        microgrids=[
            replace(
                case.microgrids[0],
                wind_available_mw={},
                wind_capacity_mw={},
                wind_capacity_mva={},
                wind_q_abs_over_p_max={},
                wind_q_abs_max_mvar={},
                storage_reactive_enabled=reactive_enabled,
            )
        ],
    )
    ctrl = controller(case, storage_enabled)
    expected = 2 * cos(pi / 16) if storage_enabled and reactive_enabled else 0
    assert maximum_support(
        ctrl, 1, [ctrl.p_charge == 0, ctrl.p_discharge == 0]
    ) == pytest.approx(expected, abs=2e-7)


@pytest.mark.parametrize("sign,expected", [(-1, 0.2), (1, 0.3)])
def test_svg_retains_asymmetric_limits_and_nameplate(sign, expected):
    case = capability_case()
    case = replace(
        case,
        microgrids=[
            replace(
                case.microgrids[0],
                svg_q_min_mvar=-0.2,
                svg_q_max_mvar=0.4,
                svg_s_max_mva=0.3,
            )
        ],
    )
    ctrl = controller(case)
    assert maximum_support(ctrl, sign, [ctrl.p_wind["W"] == 0]) == pytest.approx(
        expected, abs=2e-7
    )


def test_storage_relaxation_obeys_charge_discharge_gate_convex_hull():
    ctrl = controller(capability_case(), storage_enabled=True)
    problem = cp.Problem(
        cp.Maximize(cp.sum(ctrl.p_charge + ctrl.p_discharge)),
        ctrl.autonomous_problem.constraints,
    )
    problem.solve(solver="CLARABEL")
    assert problem.status == cp.OPTIMAL
    assert (ctrl.p_charge.value + ctrl.p_discharge.value)[0] == pytest.approx(
        2.0, abs=2e-7
    )


def test_packaged_24_point_shancheng_reference_has_executable_device_capability():
    from oilfield_energy.ac_consistency import solve_case_ac_consistent
    from oilfield_energy.admm import run_admm_coordination
    from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
    from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy

    case = build_synthetic_case(24)
    central = solve_case_ac_consistent(case, time_limit_seconds=30)
    assert central.passed
    loss_calibration = {
        name: {"p_loss_mw": row["loss_mw"], "q_loss_mvar": row["reactive_loss_mvar"]}
        for name, row in central.optimization.microgrids.items()
    }
    admm = run_admm_coordination(case, loss_calibration=loss_calibration)
    assert admm.converged
    targets = {
        "SC": {
            "p_mw": admm.p_references_mw["SC"],
            "q_mvar": admm.q_references_mvar["SC"],
        }
    }
    policy = ExecutionPolicy()
    realized = solve_case_ac_consistent(
        case,
        ["SC"],
        cluster_coordination=False,
        pcc_targets=targets,
        tracking_limits=policy.tracking_limits,
        time_limit_seconds=30,
    )
    assert realized.passed, realized.stop_reason
    row = realized.optimization.microgrids["SC"]
    for source, target, accepts in [
        ("p_grid_mw", "p_mw", policy.tracking_limits.accepts_p),
        ("q_grid_mvar", "q_mvar", policy.tracking_limits.accepts_q),
    ]:
        assert accepts(float(np.max(np.abs(row[source] - targets["SC"][target]))))

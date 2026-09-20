"""Regression cases for audit findings A01-01 and A01-02."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.ac_power_flow import backward_forward_sweep, validate_ac_dispatch
from oilfield_energy.data import Line, Storage
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.model import solve_case
from oilfield_energy.network_model import NetworkBranchKind, build_legacy_network_model_v2
from oilfield_energy.regional_control import RegionalConvexController


def boundary_case():
    case = build_synthetic_case(4)
    buses = ["PCC", "MAIN", "GEN"]
    zeros = np.zeros(4)
    mg = replace(
        case.microgrids[0], buses=buses, pcc_bus="PCC", base_mva=10.0,
        lines=[Line("FEED", "PCC", "MAIN", .001, .001, 10.),
               Line("GEN_LINE", "MAIN", "GEN", .4, .001, 1.)],
        load_p_mw={"PCC": zeros, "MAIN": np.full(4, 3.), "GEN": zeros},
        load_q_mvar={bus: zeros for bus in buses},
        wind_available_mw={"GEN": np.full(4, 1.2)},
        wind_capacity_mw={"GEN": 1.3}, wind_capacity_mva={"GEN": 1.4},
        wind_q_abs_over_p_max={"GEN": 0.0},
        pv_available_mw={}, pv_capacity_mw={}, pv_capacity_mva={},
        storage=Storage("MAIN", 0., 5., .5, 2.5, .95, .95, 1.),
        storage_reactive_enabled=False, svg_bus="MAIN",
        svg_q_min_mvar=0., svg_q_max_mvar=0.,
        network_model_v2=None, network_operating_mode_id=None,
    )
    return replace(case, microgrids=[mg])


def nameplate_case(kind):
    case = boundary_case()
    mg = replace(case.microgrids[0], lines=[
        Line("FEED", "PCC", "MAIN", .001, .001, 10.),
        Line("GEN_LINE", "MAIN", "GEN", .004, .001, 10.),
    ])
    available = np.array([.2, 1.2, .7, 1.2])
    if kind == "wind":
        mg = replace(mg, wind_available_mw={"GEN": available}, wind_capacity_mw={"GEN": 1.0})
    else:
        mg = replace(
            mg, wind_available_mw={}, wind_capacity_mw={}, wind_capacity_mva={},
            wind_q_abs_over_p_max={}, pv_available_mw={"GEN": available},
            pv_capacity_mw={"GEN": 1.0}, pv_capacity_mva={"GEN": 1.4},
            pv_reactive_enabled={"GEN": False},
        )
    return replace(case, microgrids=[mg])


class EquipmentLimitTests(unittest.TestCase):
    def assert_ac_line_limits(self, case):
        checked = solve_case_ac_consistent(
            case, ["SC"], storage_enabled=False, cluster_coordination=False,
            time_limit_seconds=30,
        )
        self.assertTrue(checked.passed, checked.ac_validation)
        self.assertEqual(checked.iterations, 1)
        dispatch = checked.optimization.microgrids["SC"]
        for t in range(4):
            ac = backward_forward_sweep(
                case.microgrids[0], dispatch["bus_p_demand_mw"][:, t],
                dispatch["bus_q_demand_mvar"][:, t],
            )
            self.assertTrue(ac["converged"])
            self.assertLessEqual(np.max(ac["line_loading_pu"]), 1.0 + 1e-6)
            np.testing.assert_allclose(
                dispatch["line_loading_pu"][:, t], ac["line_loading_pu"], atol=1e-6,
            )
            for key in ("line_sending_p_mw", "line_sending_q_mvar",
                        "line_receiving_p_mw", "line_receiving_q_mvar"):
                np.testing.assert_allclose(dispatch[key][:, t], ac[key], atol=1e-6)
        return dispatch

    def test_reverse_internal_flow_curtails_instead_of_exhausting_ac_feedback(self):
        case = boundary_case()
        dispatch = self.assert_ac_line_limits(case)
        self.assertTrue(np.all(dispatch["line_sending_p_mw"][1] < 0))
        self.assertTrue(np.all(dispatch["wind_used_mw"] < 1.0))
        # A useful curtailed solution, not zero generation or modified input data.
        self.assertTrue(np.all(dispatch["wind_used_mw"] > .97))
        np.testing.assert_array_equal(case.microgrids[0].wind_available_mw["GEN"], np.full(4, 1.2))

    def test_forward_flow_preserves_sending_end_limit(self):
        case = boundary_case()
        mg = case.microgrids[0]
        mg = replace(mg, wind_available_mw={"GEN": np.zeros(4)},
                     load_p_mw={**mg.load_p_mw, "GEN": np.full(4, .9)})
        dispatch = self.assert_ac_line_limits(replace(case, microgrids=[mg]))
        self.assertTrue(np.all(dispatch["line_sending_p_mw"][1] > 0))
        self.assertTrue(np.all(dispatch["line_sending_p_mw"][1] > dispatch["line_receiving_p_mw"][1]))

    def test_off_nominal_transformer_both_declared_directions(self):
        case = boundary_case()
        mg = case.microgrids[0]
        network = build_legacy_network_model_v2(
            network_id="audit-transformer", buses=mg.buses, pcc_bus_id=mg.pcc_bus,
            base_mva=mg.base_mva, lines=mg.lines,
        )
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                transformer = replace(
                    network.branches[1], kind=NetworkBranchKind.TRANSFORMER,
                    fixed_tap_ratio=1.01, from_bus_id="GEN" if reverse else "MAIN",
                    to_bus_id="MAIN" if reverse else "GEN",
                )
                modified = replace(network, branches=(network.branches[0], transformer))
                self.assert_ac_line_limits(replace(case, microgrids=[replace(mg, network_model_v2=modified)]))

    def test_legacy_model_reports_and_limits_both_modeled_ends(self):
        result = solve_case(boundary_case(), ["SC"], storage_enabled=False, cluster_coordination=False)
        self.assertTrue(result.success, result.message)
        dispatch = result.microgrids["SC"]
        send = np.hypot(dispatch["line_sending_p_mw"], dispatch["line_sending_q_mvar"])
        receive = np.hypot(dispatch["line_receiving_p_mw"], dispatch["line_receiving_q_mvar"])
        np.testing.assert_allclose(dispatch["line_loading_pu"], np.maximum(send, receive) / np.array([[10.], [1.]]))
        self.assertLessEqual(np.max(dispatch["line_loading_pu"]), 1.0 + 1e-6)
        self.assertTrue(np.all(dispatch["wind_used_mw"] < 1.0))

    def test_wind_and_pv_limits_apply_to_both_solvers_without_changing_forecasts(self):
        for kind in ("wind", "pv"):
            for solver in (solve_case_misocp, solve_case):
                with self.subTest(kind=kind, solver=solver.__name__):
                    case = nameplate_case(kind)
                    forecast = getattr(case.microgrids[0], f"{kind}_available_mw")["GEN"]
                    original = forecast.copy()
                    result = solver(case, ["SC"], storage_enabled=False, cluster_coordination=False)
                    self.assertTrue(result.success, result.message)
                    np.testing.assert_allclose(result.microgrids["SC"][f"{kind}_used_mw"], [.2, 1., .7, 1.], atol=1e-6)
                    np.testing.assert_array_equal(forecast, original)
                    # Output retains the original availability, including the over-rating input.
                    np.testing.assert_array_equal(result.microgrids["SC"][f"{kind}_available_mw"], original)

    def test_regional_coordination_uses_same_nameplate_bounds(self):
        for kind in ("wind", "pv"):
            with self.subTest(kind=kind):
                case = nameplate_case(kind)
                controller = RegionalConvexController(case, case.microgrids[0], ADMMConfig())
                schedule = controller.solve(None)
                np.testing.assert_allclose(schedule.renewable_used_mw, [.2, 1., .7, 1.], atol=1e-5)

    def test_independent_validation_rejects_out_of_rating_and_missing_plans(self):
        for kind in ("wind", "pv"):
            case = nameplate_case(kind)
            checked = solve_case_ac_consistent(case, ["SC"], storage_enabled=False, cluster_coordination=False)
            self.assertTrue(checked.passed)
            for invalid in ("over_rated", "over_available", "negative", "nonfinite", "missing"):
                with self.subTest(kind=kind, invalid=invalid):
                    result = deepcopy(checked.optimization)
                    values = result.microgrids["SC"][f"{kind}_active_by_bus_mw"]
                    if invalid == "missing":
                        values.clear()
                    else:
                        profile = np.array(values["GEN"], copy=True)
                        if invalid == "over_rated":
                            profile[1] = 1.2
                        elif invalid == "over_available":
                            profile[0] = .5
                        elif invalid == "negative":
                            profile[0] = -.1
                        else:
                            profile[0] = float("nan")
                        values["GEN"] = profile
                    report = validate_ac_dispatch(case, result, ["SC"])
                    self.assertTrue(report["microgrids"]["SC"]["all_converged"])
                    self.assertFalse(report["microgrids"]["SC"]["renewable_active_power_compliant"])
                    self.assertFalse(report["passed"])

    def test_invalid_ratings_and_availability_are_rejected_before_solving(self):
        for kind in ("wind", "pv"):
            for solver in (solve_case_misocp, solve_case):
                for invalid in ("missing_rating", "negative_rating", "nan_rating", "inf_rating", "negative_forecast", "nan_forecast", "short_forecast"):
                    with self.subTest(kind=kind, solver=solver.__name__, invalid=invalid):
                        case = nameplate_case(kind)
                        mg = case.microgrids[0]
                        if invalid.endswith("rating"):
                            rating = {"missing_rating": None, "negative_rating": -1., "nan_rating": float("nan"), "inf_rating": float("inf")}[invalid]
                            changes = {f"{kind}_capacity_mw": {} if rating is None else {"GEN": rating}}
                        else:
                            values = np.full(3 if invalid == "short_forecast" else 4, .5)
                            if invalid != "short_forecast":
                                values[0] = -1. if invalid == "negative_forecast" else float("nan")
                            changes = {f"{kind}_available_mw": {"GEN": values}}
                        with self.assertRaises(ValueError):
                            solver(replace(case, microgrids=[replace(mg, **changes)]), ["SC"])


if __name__ == "__main__":
    unittest.main()

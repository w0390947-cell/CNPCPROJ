from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.network_model import (
    NetworkBranchKind, NetworkBus, NetworkContingency, NetworkDataProvenance,
    NetworkModelV2, NetworkOperatingMode, NetworkSwitch, SeriesBranch, SwitchState,
    TransformerTapChanger, TransformerTapPosition,
    assess_network_model,
)
from oilfield_energy.network_scenarios import (
    NetworkOperatingPoint, NetworkScenario, NetworkSecurityLimits, ScenarioStatus,
    build_network_scenarios, evaluate_network_scenarios, write_network_scenario_outputs,
)


def network_fixture():
    # A侧故障后只允许调用方明确指定的母联转供。资产顺序刻意不按拓扑排列。
    return NetworkModelV2(
        network_id="transfer-test", base_mva=10, pcc_bus_id="PCC",
        buses=tuple(NetworkBus(b, b, 10) for b in ("PCC", "A", "B")),
        branches=(
            SeriesBranch("TIE", "B", "A", NetworkBranchKind.LINE, .005, .008, 3),
            SeriesBranch("LA", "PCC", "A", NetworkBranchKind.LINE, .01, .02, 4),
            SeriesBranch("LB", "PCC", "B", NetworkBranchKind.LINE, .01, .02, 4),
        ),
        switches=(NetworkSwitch("S_TIE", "TIE", normally_closed=False),),
        operating_modes=(NetworkOperatingMode("normal", "two radial feeders"),
                         NetworkOperatingMode("transfer", "close tie after outage",
                                              switch_states=(SwitchState("S_TIE", True),))),
        default_operating_mode_id="normal",
        contingencies=(NetworkContingency("lose-A", ("LA",)),
                       NetworkContingency("lose-B", ("LB",)),
                       NetworkContingency("lose-tie", ("TIE",))),
        provenance=NetworkDataProvenance("synthetic test fixture", "1", synthetic=True),
    )


def point_fixture(point_id="noon"):
    return NetworkOperatingPoint(point_id, "synthetic net demand",
                                 {"B": .7, "PCC": .1, "A": 1.0},
                                 {"A": .2, "B": .1, "PCC": .02},
                                 quality_valid=True, coherent=True)


class NetworkScenarioTests(unittest.TestCase):
    def setUp(self):
        self.model = network_fixture()
        self.point = point_fixture()
        self.limits = NetworkSecurityLimits(.95, 1.05, 0, 6, .90)

    def run_scenarios(self, scenarios, *, model=None, points=None, limits=None):
        return evaluate_network_scenarios(model or self.model, points or (self.point,),
                                          scenarios, limits or self.limits)

    def test_explicit_transfer_retains_outage_stage_and_same_demands(self):
        request = NetworkScenario("A-transfer", "normal", "lose-A", "transfer")
        before = repr(self.model)
        batch = self.run_scenarios((request,))
        result = batch.results[0]
        self.assertEqual(result.status, ScenarioStatus.TRANSFER_SECURE)
        self.assertEqual(result.stages[0].status, ScenarioStatus.ISLANDED)
        self.assertEqual(result.stages[0].disconnected_bus_ids, ("A",))
        self.assertEqual(result.stages[0].points, ())
        self.assertEqual(result.stages[1].active_branch_ids, ("TIE", "LB"))
        self.assertNotIn("LA", result.stages[1].active_branch_ids)
        point = result.stages[1].points[0]
        self.assertAlmostEqual(point.pcc_import_mw, 1.8 + point.loss_mw, places=8)
        self.assertEqual(point.point_id, self.point.point_id)
        self.assertTrue(batch.all_final_states_secure)
        self.assertFalse(batch.all_immediate_states_secure)
        self.assertEqual(repr(self.model), before)

    def test_transfer_topology_does_not_imply_capacity_compliance(self):
        overloaded = replace(self.model, branches=tuple(
            replace(b, s_max_mva=.8) if b.branch_id == "LB" else b
            for b in self.model.branches
        ))
        result = self.run_scenarios(
            (NetworkScenario("A-transfer", "normal", "lose-A", "transfer"),), model=overloaded,
        ).results[0]
        self.assertEqual(result.status, ScenarioStatus.VIOLATION)
        self.assertEqual(result.stages[-1].disconnected_bus_ids, ())
        violation = result.stages[-1].points[0].violations[0]
        self.assertEqual((violation.code, violation.asset_id), ("BRANCH_OVERLOAD", "LB"))

    def test_two_radial_operating_modes_apply_distinct_fixed_taps(self):
        transformer = replace(
            self.model.branches[1], kind=NetworkBranchKind.TRANSFORMER,
            tap_changer=TransformerTapChanger(
                minimum_position=-2, maximum_position=2, neutral_position=0,
                default_position=0, step_percent=1.0,
            ),
        )
        alternate = NetworkOperatingMode(
            "tap-high", "different fixed tap", transformer_taps=(TransformerTapPosition("LA", 1),),
        )
        model = replace(self.model, branches=(transformer, self.model.branches[2]),
                        switches=(), contingencies=(),
                        operating_modes=(self.model.operating_modes[0], alternate))
        batch = self.run_scenarios(build_network_scenarios(model), model=model)
        self.assertEqual(len(batch.results), 2)
        self.assertTrue(batch.all_final_states_secure)
        flows = [item.stages[0].points[0] for item in batch.results]
        self.assertLess(flows[1].buses[1].voltage_pu, flows[0].buses[1].voltage_pu)
        self.assertEqual(flows[1].branches[0].tap_ratio, 1.01)

    def test_solver_and_readiness_share_zero_phase_tolerance(self):
        model = replace(self.model, branches=tuple(
            replace(b, phase_shift_degrees=1e-13) if b.branch_id == "LA" else b
            for b in self.model.branches
        ))
        batch = self.run_scenarios((NetworkScenario("tiny-phase", "normal"),), model=model)
        self.assertTrue(batch.all_final_states_secure)

    def test_batch_isolates_unsupported_unknown_and_islanded_scenarios(self):
        result = self.run_scenarios((
            NetworkScenario("normal", "normal"), NetworkScenario("mesh", "transfer"),
            NetworkScenario("unknown", "missing"), NetworkScenario("outage", "normal", "lose-A"),
        ))
        self.assertEqual([r.status for r in result.results], [ScenarioStatus.SECURE,
                         ScenarioStatus.UNSUPPORTED, ScenarioStatus.INVALID_INPUT, ScenarioStatus.ISLANDED])
        self.assertFalse(result.all_final_states_secure)

    def test_missing_bad_and_nonfinite_points_have_no_numeric_capability(self):
        bad = (
            replace(self.point, point_id="missing", p_demand_mw_by_bus={"A": 1}),
            replace(self.point, point_id="nan", q_demand_mvar_by_bus={"PCC": 0, "A": np.nan, "B": 0}),
            replace(self.point, point_id="bad", quality_valid=False),
            replace(self.point, point_id="incoherent", coherent=False),
        )
        batch = self.run_scenarios((NetworkScenario("normal", "normal"),), points=(*bad, self.point))
        samples = batch.results[0].stages[0].points
        for sample in samples[:-1]:
            self.assertEqual(sample.status, ScenarioStatus.INVALID_INPUT)
            self.assertIsNone(sample.pcc_import_mw)
            self.assertEqual(sample.buses, ())
        self.assertEqual(samples[-1].status, ScenarioStatus.SECURE)

    def test_nonconvergence_not_reported_as_secure_or_zero_power(self):
        batch = self.run_scenarios((NetworkScenario("normal", "normal"),),
                                  limits=replace(self.limits, max_iterations=1))
        sample = batch.results[0].stages[0].points[0]
        self.assertEqual(sample.status, ScenarioStatus.NOT_CONVERGED)
        self.assertIsNone(sample.pcc_import_mw)
        self.assertEqual(sample.branches, ())

    def test_registered_n1_coverage_and_already_open_branch(self):
        requests = build_network_scenarios(self.model, ("normal",),
                                          recovery_modes={("normal", "lose-A"): "transfer"})
        batch = self.run_scenarios(requests)
        self.assertTrue(batch.n_minus_one_coverage_complete)
        self.assertEqual(batch.results[-1].status, ScenarioStatus.NOT_APPLICABLE)
        self.assertFalse(batch.all_final_states_secure)
        partial = self.run_scenarios(requests[:2])
        self.assertFalse(partial.n_minus_one_coverage_complete)
        self.assertIn("normal:LB", partial.uncovered_n_minus_one)
        baseline_only = self.run_scenarios((requests[0],))
        self.assertFalse(baseline_only.n_minus_one_coverage_complete)

    def test_unknown_disabled_contingency_and_unknown_recovery_are_invalid(self):
        model = replace(self.model, contingencies=(replace(self.model.contingencies[0], enabled=False),))
        batch = self.run_scenarios((NetworkScenario("disabled", "normal", "lose-A"),
                                   NetworkScenario("missing", "normal", "unknown")), model=model)
        self.assertTrue(all(r.status is ScenarioStatus.INVALID_INPUT for r in batch.results))
        invalid_recovery = self.run_scenarios((NetworkScenario("x", "normal", "lose-A", "unknown"),))
        self.assertEqual(invalid_recovery.results[0].status, ScenarioStatus.INVALID_INPUT)
        self.assertEqual(invalid_recovery.results[0].stages[0].status, ScenarioStatus.ISLANDED)

    def test_voltage_import_and_pf_violation_codes_carry_asset_identity(self):
        strict = replace(self.limits, voltage_min_pu=.9999, power_factor_min=.999,
                         pcc_import_max_mw=1.0)
        sample = self.run_scenarios((NetworkScenario("normal", "normal"),),
                                    limits=strict).results[0].stages[0].points[0]
        self.assertTrue({"VOLTAGE_LOW", "PCC_IMPORT_HIGH", "PCC_POWER_FACTOR_LOW"}.issubset(
            {v.code for v in sample.violations}))
        reverse = replace(self.point, p_demand_mw_by_bus={"PCC": 0, "A": -1, "B": 0})
        sample = self.run_scenarios((NetworkScenario("normal", "normal"),),
                                    points=(reverse,)).results[0].stages[0].points[0]
        self.assertIn("PCC_IMPORT_LOW", {v.code for v in sample.violations})

    def test_reverse_transformer_loss_matches_original_winding_reference(self):
        a, r = 1.05, .02
        model = replace(self.model, branches=(
            SeriesBranch("LA", "A", "PCC", NetworkBranchKind.TRANSFORMER,
                         r, .04, 4, fixed_tap_ratio=a),
            self.model.branches[-1],
        ), switches=(), operating_modes=(self.model.operating_modes[0],), contingencies=())
        resolved = assess_network_model(model).require_current_solver_ready()
        flow = backward_forward_sweep_resolved(resolved, np.array([0, 1, 0]), np.array([0, .2, 0]))
        voltage_a = flow["voltage_pu"][1]
        # 台账 R 位于PCC侧，反向时A侧阻抗必须乘a²；独立于解析器的结果计算。
        expected_loss = a*a*r*(1.0**2 + .2**2) / (10 * voltage_a**2)
        self.assertAlmostEqual(flow["loss_mw"], expected_loss, places=9)
        self.assertAlmostEqual(flow["pcc_p_mw"], 1 + expected_loss, places=8)

    def test_receiving_terminal_overload_is_checked_under_reverse_flow(self):
        model = replace(self.model, branches=(
            replace(self.model.branches[1], r_pu=.1, x_pu=.1, s_max_mva=.995),
            self.model.branches[2],
        ), switches=(), operating_modes=(self.model.operating_modes[0],), contingencies=())
        point = replace(self.point, p_demand_mw_by_bus={"PCC": 0, "A": -1, "B": 0},
                        q_demand_mvar_by_bus={"PCC": 0, "A": 0, "B": 0})
        sample = self.run_scenarios((NetworkScenario("x", "normal"),), model=model,
                                    points=(point,)).results[0].stages[0].points[0]
        branch = sample.branches[0]
        self.assertLess(np.hypot(branch.sending_p_mw, branch.sending_q_mvar), .995)
        self.assertGreater(branch.loading_pu, 1)
        self.assertIn("BRANCH_OVERLOAD", {v.code for v in sample.violations})

    def test_report_export_preserves_null_failure_states_and_input_snapshot(self):
        points = (self.point, replace(self.point, point_id="bad", quality_valid=False))
        batch = self.run_scenarios(build_network_scenarios(self.model, ("normal",)), points=points)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            write_network_scenario_outputs(output, batch, model=self.model, points=points)
            report = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(report["field_acceptance_certified"])
            sample = report["results"][0]["stages"][0]["points"][1]
            self.assertIsNone(sample["pcc_import_mw"])
            self.assertTrue((output / "inputs.json").is_file())
            with (output / "points.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(any(row["status"] == "islanded" and row["pcc_import_mw"] == "" for row in rows))
            self.assertTrue(any(row["status"] == "not_applicable" for row in rows))

    def test_no_empty_success_duplicate_identity_or_implicit_recovery(self):
        with self.assertRaises(ValueError):
            evaluate_network_scenarios(self.model, (), (), self.limits)
        with self.assertRaises(ValueError):
            self.run_scenarios((NetworkScenario("same", "normal"), NetworkScenario("same", "transfer")))
        with self.assertRaises(ValueError):
            NetworkScenario("recovery", "normal", recovery_operating_mode_id="transfer")
        with self.assertRaises(ValueError):
            build_network_scenarios(self.model, ("normal",), recovery_modes={("normal", "unknown"): "transfer"})


if __name__ == "__main__":
    unittest.main()

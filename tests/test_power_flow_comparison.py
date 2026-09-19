from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from math import sqrt
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.network_model import (
    NetworkBranchKind, NetworkBus, NetworkPhaseModel, SeriesBranch,
    ShuntCompensator, ShuntKind, assess_network_model,
)
from oilfield_energy.power_flow_comparison import (
    BusLoadState, DevicePerturbation, compare_fixed_states, read_comparison_request,
    write_comparison_outputs,
)

EXAMPLE = ROOT / "docs/examples/fixed_device_comparison.json"


class FixedStateComparisonTests(unittest.TestCase):
    def setUp(self):
        self.request = read_comparison_request(EXAMPLE)

    def test_only_selected_device_changes_and_net_demand_is_conservative(self):
        original = asdict(self.request)
        with patch("oilfield_energy.model.solve_case", side_effect=AssertionError("optimizer called")), \
             patch("oilfield_energy.misocp_model.solve_case_misocp", side_effect=AssertionError("optimizer called")), \
             patch("oilfield_energy.ac_consistency.solve_case_ac_consistent", side_effect=AssertionError("redispatch")), \
             patch("oilfield_energy.data.build_synthetic_case", side_effect=AssertionError("synthetic fallback")):
            result = compare_fixed_states(self.request)
        self.assertEqual(result["status"], "secure")
        self.assertEqual(asdict(self.request), original)
        self.assertFalse(result["redispatch_performed"])
        after_devices = {d["resource_id"]: d for d in result["changed_snapshot"]["devices"]}
        for d in self.request.snapshot.devices:
            expected = asdict(replace(d, p_mw=d.p_mw + 1)) if d.resource_id == "WT1" else asdict(d)
            self.assertEqual(after_devices[d.resource_id], expected)
        first, second = result["operating_points"]
        self.assertEqual(first["q_demand_mvar_by_bus"], second["q_demand_mvar_by_bus"])
        self.assertEqual(first["p_demand_mw_by_bus"]["WIND"] - second["p_demand_mw_by_bus"]["WIND"], 1)
        self.assertEqual(first["p_demand_mw_by_bus"]["MAIN"], 4.7)
        for stage, point in zip(("before", "after"), (first, second)):
            flow = result[stage]
            self.assertAlmostEqual(flow["pcc_import_mw"], sum(point["p_demand_mw_by_bus"].values()) + flow["loss_mw"], places=8)
        changes = {r["quantity"]: r["delta"] for r in result["differences"]["system"]}
        self.assertAlmostEqual(changes["pcc_import_mw"], -1 + changes["loss_mw"], places=8)

    def test_zero_perturbation_has_zero_differences(self):
        result = compare_fixed_states(replace(self.request, perturbation=replace(self.request.perturbation, delta=0)))
        for kind in ("buses", "branches"):
            for row in result["differences"][kind]:
                self.assertTrue(all(value == 0 for key, value in row.items() if key.startswith("delta_")))
        self.assertTrue(all(row["delta"] == 0 for row in result["differences"]["system"]))

    def test_q_perturbation_holds_every_active_injection(self):
        result = compare_fixed_states(replace(self.request, perturbation=DevicePerturbation("SVG1", "q_mvar", .2)))
        first, second = result["operating_points"]
        self.assertEqual(result["held_quantity"], "p_mw")
        self.assertEqual(first["p_demand_mw_by_bus"], second["p_demand_mw_by_bus"])
        self.assertAlmostEqual(first["q_demand_mvar_by_bus"]["MAIN"] - second["q_demand_mvar_by_bus"]["MAIN"], .2)

    def test_transformer_voltage_and_both_terminal_currents_against_analytic_solution(self):
        # Pure resistance, one constant-P load: V^2 - (Vslack/tap)V + RP/Sbase = 0.
        r, tap, boundary, p = .02, 1.05, 1.04, 1.0
        model = replace(self.request.network,
                        buses=(NetworkBus("PCC", "PCC", 35), NetworkBus("B", "B", 10)),
                        branches=(SeriesBranch("T", "PCC", "B", NetworkBranchKind.TRANSFORMER,
                                               r, 0, 5, fixed_tap_ratio=tap),))
        snap = replace(self.request.snapshot, slack_voltage_pu=boundary,
                       loads=(BusLoadState("PCC", 0, 0), BusLoadState("B", p, 0)),
                       devices=(replace(self.request.snapshot.devices[0], bus_id="B", p_mw=0, q_mvar=0),))
        request = replace(self.request, network=model, snapshot=snap,
                          perturbation=DevicePerturbation("WT1", "p_mw", .1))
        result = compare_fixed_states(request)
        v = (boundary/tap + sqrt((boundary/tap)**2 - 4*r*p/model.base_mva)) / 2
        receiving_a = p*1000/(sqrt(3)*10*v)
        branch = result["before"]["branches"][0]
        self.assertAlmostEqual(result["before"]["buses"][0]["voltage_kv"], 35*boundary)
        self.assertAlmostEqual(result["before"]["buses"][1]["voltage_pu"], v, places=9)
        self.assertAlmostEqual(branch["receiving_current_a"], receiving_a, places=6)
        self.assertAlmostEqual(branch["sending_current_a"], receiving_a*10/(35*tap), places=6)
        self.assertAlmostEqual(branch["active_loss_mw"], r*p*p/(model.base_mva*v*v), places=8)
        # Reverse ledger orientation must describe exactly the same physical transformer.
        reversed_model = replace(model, branches=(replace(model.branches[0],
            from_bus_id="B", to_bus_id="PCC", fixed_tap_ratio=1/tap, r_pu=r*tap*tap),))
        reversed_result = compare_fixed_states(replace(request, network=reversed_model))
        for key in ("sending_current_a", "receiving_current_a", "active_loss_mw"):
            self.assertAlmostEqual(branch[key], reversed_result["before"]["branches"][0][key], places=7)

    def test_shunt_configuration_is_fixed_but_q_responds_to_voltage(self):
        shunt = ShuntCompensator("C1", "WIND", ShuntKind.CAPACITOR, .2, 0, 1, 1)
        model = replace(self.request.network, shunts=(shunt,))
        result = compare_fixed_states(replace(self.request, network=model))
        self.assertEqual(model.shunts[0].default_steps, 1)
        for stage in ("before", "after"):
            bus = next(b for b in result[stage]["buses"] if b["bus_id"] == "WIND")
            self.assertAlmostEqual(bus["fixed_shunt_q_mvar"], .2*bus["voltage_pu"]**2)
        row = next(b for b in result["differences"]["buses"] if b["bus_id"] == "WIND")
        self.assertNotEqual(row["delta_fixed_shunt_q_mvar"], 0)

    def test_reverse_flow_overload_voltage_and_capability_are_reported_not_corrected(self):
        model = replace(self.request.network, branches=tuple(replace(b, s_max_mva=.1) for b in self.request.network.branches))
        request = replace(self.request, network=model,
                          limits=replace(self.request.limits, voltage_max_pu=1.001),
                          perturbation=replace(self.request.perturbation, delta=5))
        result = compare_fixed_states(request)
        self.assertEqual(result["status"], "violation")
        self.assertEqual(result["changed_snapshot"]["devices"][0]["p_mw"], 6)
        self.assertEqual(result["pcc_direction"]["after"], "export")
        codes = {v["code"] for v in result["after"]["violations"]}
        self.assertTrue({"PCC_IMPORT_LOW", "BRANCH_OVERLOAD", "VOLTAGE_HIGH"}.issubset(codes))
        self.assertEqual(result["device_violations"]["after"][0]["code"], "P_MAX")
        self.assertIsNotNone(result["differences"])

    def test_nonconvergence_keeps_valid_baseline_without_fake_deltas(self):
        # Empty-net baseline converges at initialization; a large perturbation cannot converge in 1 step.
        model = replace(self.request.network, branches=tuple(replace(b, fixed_tap_ratio=1) for b in self.request.network.branches))
        snap = replace(self.request.snapshot, loads=tuple(replace(x, p_mw=0, q_mvar=0) for x in self.request.snapshot.loads),
                       devices=tuple(replace(x, p_mw=0, q_mvar=0) for x in self.request.snapshot.devices))
        result = compare_fixed_states(replace(self.request, network=model, snapshot=snap,
                                             limits=replace(self.request.limits, max_iterations=1)))
        self.assertEqual(result["before"]["status"], "secure")
        self.assertEqual(result["after"]["status"], "not_converged")
        self.assertIsNone(result["after"]["pcc_import_mw"])
        self.assertIsNone(result["differences"])

    def test_bad_mapping_unknown_offline_quality_and_missing_voltage_are_blocked(self):
        snap = self.request.snapshot
        variants = (
            replace(self.request, snapshot=replace(snap, loads=snap.loads[:-1])),
            replace(self.request, snapshot=replace(snap, devices=(replace(snap.devices[0], bus_id="unknown"),))),
            replace(self.request, perturbation=replace(self.request.perturbation, resource_id="unknown")),
            replace(self.request, snapshot=replace(snap, devices=(replace(snap.devices[0], p_mw=0, in_service=False),))),
            replace(self.request, snapshot=replace(snap, quality_valid=False)),
            replace(self.request, snapshot=replace(snap, coherent=False)),
            replace(self.request, network=replace(self.request.network, buses=tuple(replace(b, nominal_voltage_kv=None) for b in self.request.network.buses))),
            replace(self.request, require_field_approval=True),
        )
        for request in variants:
            with self.subTest(request=request):
                report = compare_fixed_states(request)
                self.assertEqual(report["status"], "invalid_input")
                self.assertTrue(report["reasons"])
                self.assertIsNone(report["differences"])
        with self.assertRaises(ValueError):
            replace(snap, devices=(snap.devices[0], snap.devices[0]))

    def test_unsupported_and_disconnected_networks_remain_explicit(self):
        for model, expected in (
            (replace(self.request.network, phase_model=NetworkPhaseModel.THREE_PHASE_UNBALANCED), "unsupported"),
            (replace(self.request.network, branches=self.request.network.branches[:1]), "islanded"),
            (replace(self.request.network, branches=(*self.request.network.branches,
                SeriesBranch("tie", "PCC", "WIND", NetworkBranchKind.LINE, .01, .02, 5))), "unsupported"),
        ):
            report = compare_fixed_states(replace(self.request, network=model))
            self.assertEqual(report["status"], expected)
            self.assertIsNone(report["differences"])

    def test_strict_file_parsing_rejects_ambiguity_and_invalid_values(self):
        original = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        mutations = (
            lambda x: x["snapshot"].update(slack_voltage_pu="1.01"),
            lambda x: x["snapshot"].update(slack_voltage_pu=True),
            lambda x: x["snapshot"].update(slack_voltage_pu=float("nan")),
            lambda x: x["snapshot"].update(at="2026-09-13T12:00:00"),
            lambda x: x["snapshot"].update(load_basis="net_feeder_exchange"),
            lambda x: x["snapshot"]["devices"][0].update(p_mw=None),
            lambda x: x["network"]["branches"][0].update(misspelled_tap=1.02),
            lambda x: x["network"].update(unrecognized=True),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"input.json"
            for mutate in mutations:
                data = json.loads(json.dumps(original))
                mutate(data)
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_comparison_request(path)
            path.write_text('{"schema_version":"a", "schema_version":"b"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                read_comparison_request(path)

    def test_export_replay_and_failed_rerun_remove_old_difference_rows(self):
        result = compare_fixed_states(self.request)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_comparison_outputs(output, self.request, result)
            replay = read_comparison_request(output/"request.json")
            self.assertEqual(compare_fixed_states(replay), result)
            saved = json.loads((output/"comparison.json").read_text(encoding="utf-8"))
            self.assertFalse(saved["field_acceptance_certified"])
            with (output/"branches_comparison.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertIn("delta_sending_current_a", rows[0])
            failed = replace(self.request, snapshot=replace(self.request.snapshot, quality_valid=False))
            write_comparison_outputs(output, failed, compare_fixed_states(failed))
            with (output/"branches_comparison.csv").open(encoding="utf-8-sig") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

    def test_cli_runs_and_reports_bad_requests_without_starting_optimization(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(ROOT/"run_model.py"), "compare-power-flow", "--output", directory]
            result = subprocess.run([*command, "--input", str(EXAMPLE)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("redispatch=False", result.stdout)
            bad = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)
            self.assertIn("requires --input", bad.stderr)

    def test_core_rejects_invalid_boundary_voltage(self):
        net = assess_network_model(self.request.network).require_current_solver_ready()
        for bad in (0, -1, float("nan"), True):
            with self.assertRaises(ValueError):
                backward_forward_sweep_resolved(net, np.zeros(3), np.zeros(3), slack_voltage_pu=bad)

    def test_finite_measurements_that_overflow_on_aggregation_do_not_export_infinity(self):
        snap = self.request.snapshot
        bad = replace(self.request, snapshot=replace(snap,
            loads=tuple(replace(x, p_mw=1e308) for x in snap.loads),
            devices=(replace(snap.devices[0], p_mw=-1e308),)))
        result = compare_fixed_states(bad)
        self.assertEqual(result["status"], "invalid_input")
        self.assertIsNone(result["differences"])
        with tempfile.TemporaryDirectory() as directory:
            write_comparison_outputs(Path(directory), bad, result)
            text = (Path(directory)/"comparison.json").read_text(encoding="utf-8")
            self.assertNotIn("Infinity", text)


if __name__ == "__main__":
    unittest.main()

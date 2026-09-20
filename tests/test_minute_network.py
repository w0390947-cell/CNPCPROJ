from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oilfield_energy.actuation_arbiter import ActuationArbiter
from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.control_contracts import CapIntentAction, StationCapIntent
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.hierarchy_reporting import write_minute_network_outputs
from oilfield_energy.minute_network import MinuteNetworkConfig, MinuteNetworkEvaluator, NetworkRecoveryInterlock
from oilfield_energy.network_model import NetworkPhaseModel
from oilfield_energy.network_scenarios import NetworkSecurityLimits
from oilfield_energy.power_flow_comparison import read_comparison_request


class MinuteNetworkTests(unittest.TestCase):
    def setUp(self):
        self.request = read_comparison_request(ROOT / "tests/fixtures/field_dataset/fixed_device_comparison.json")
        self.snapshot = self.request.snapshot
        self.evaluator = MinuteNetworkEvaluator(self.request.network, self.request.limits,
            {d.resource_id: d.bus_id for d in self.snapshot.devices})

    def feedback(self, minute=0, *, snapshot=None, evaluator=None):
        now = self.snapshot.at + timedelta(minutes=minute)
        return (evaluator or self.evaluator).evaluate(snapshot or replace(self.snapshot, at=now),
                                                     at=now, operating_mode_id="normal")

    def test_actual_per_device_mapping_gross_loads_and_pcc_conservation(self):
        with patch("oilfield_energy.model.solve_case", side_effect=AssertionError("redispatch")):
            result = self.feedback()
        self.assertTrue(result.valid)
        self.assertTrue(result.recovery_safe)
        p_net = result.inputs["p_demand_mw_by_bus"]
        self.assertAlmostEqual(p_net["WIND"], -1)
        self.assertAlmostEqual(p_net["MAIN"], 4.7)
        self.assertAlmostEqual(result.flow.pcc_import_mw, sum(p_net.values()) + result.flow.loss_mw, places=8)
        changed = replace(self.snapshot, devices=tuple(replace(d, p_mw=d.p_mw + 1) if d.resource_id == "WT2" else d for d in self.snapshot.devices))
        second = self.feedback(snapshot=changed)
        self.assertAlmostEqual(second.inputs["p_demand_mw_by_bus"]["WIND"], -2)
        self.assertEqual(result.inputs["snapshot"]["devices"][0], second.inputs["snapshot"]["devices"][0])

    def test_unknown_is_not_a_safe_or_measured_violation(self):
        variants = (
            replace(self.snapshot, quality_valid=False),
            replace(self.snapshot, coherent=False),
            replace(self.snapshot, at=self.snapshot.at - timedelta(seconds=61)),
            replace(self.snapshot, at=self.snapshot.at + timedelta(seconds=1)),
            replace(self.snapshot, devices=self.snapshot.devices[:-1]),
            replace(self.snapshot, loads=self.snapshot.loads[:-1]),
        )
        for snapshot in variants:
            with self.subTest(snapshot=snapshot):
                result = self.feedback(snapshot=snapshot)
                self.assertFalse(result.valid)
                self.assertFalse(result.validity.valid)
                self.assertIsNone(result.voltage_within_limits)
                self.assertIsNone(result.line_capacity_within_limits)
                self.assertFalse(result.recovery_safe)
                self.assertTrue(result.reasons)

    def test_nonconvergence_unsupported_island_and_field_approval_fail_closed(self):
        variants = (
            MinuteNetworkEvaluator(self.request.network, replace(self.request.limits, max_iterations=1), self.evaluator.device_buses),
            MinuteNetworkEvaluator(replace(self.request.network, phase_model=NetworkPhaseModel.THREE_PHASE_UNBALANCED), self.request.limits, self.evaluator.device_buses),
            MinuteNetworkEvaluator(replace(self.request.network, branches=self.request.network.branches[:1]), self.request.limits, self.evaluator.device_buses),
            MinuteNetworkEvaluator(self.request.network, self.request.limits, self.evaluator.device_buses, config=MinuteNetworkConfig(require_field_approval=True)),
            MinuteNetworkEvaluator(None, self.request.limits, self.evaluator.device_buses),
        )
        for evaluator, status in zip(variants, ("not_converged", "unsupported", "islanded", "invalid_input", "invalid_input")):
            result = self.feedback(evaluator=evaluator)
            self.assertEqual(result.status, status)
            self.assertFalse(result.recovery_safe)

    def test_voltage_and_overload_are_valid_unsafe_measurements(self):
        model = replace(self.request.network, branches=tuple(replace(b, s_max_mva=.01) for b in self.request.network.branches))
        evaluator = MinuteNetworkEvaluator(model, replace(self.request.limits, voltage_max_pu=1.001), self.evaluator.device_buses)
        result = self.feedback(evaluator=evaluator)
        self.assertTrue(result.valid)
        self.assertTrue(result.validity.valid)
        self.assertFalse(result.voltage_within_limits)
        self.assertFalse(result.line_capacity_within_limits)
        self.assertFalse(result.recovery_safe)
        self.assertTrue(result.flow.violations)

    def test_interlock_cancels_inflight_recovery_and_requires_fresh_explicit_rearm(self):
        arbiter, gate = ActuationArbiter(), NetworkRecoveryInterlock()
        available = {"pv": 4.0}
        arbiter.apply(available, (StationCapIntent("group_control", "pv", CapIntentAction.SET_CAP, 3.0),))
        bad = self.feedback(snapshot=replace(self.snapshot, quality_valid=False))
        gate.update(bad, arbiter, available, {"pv": 2.0})
        self.assertTrue(gate.blocked)
        self.assertEqual(arbiter.resolve(available)[0].effective_cap_mw, 2.0)
        # An already issued owner release cannot override the network freeze.
        arbiter.apply(available, (StationCapIntent("group_control", "pv", CapIntentAction.RELEASE_CAP),))
        self.assertEqual(arbiter.resolve(available)[0].effective_cap_mw, 2.0)
        first = self.feedback(1)
        self.assertFalse(gate.update(first, arbiter, available, {"pv": 2.0}, rearm_requested=True))
        # Reusing the same observation with a later evaluation cannot count twice.
        reused = replace(first, evaluated_at=first.evaluated_at + timedelta(seconds=20))
        self.assertFalse(gate.update(reused, arbiter, available, {"pv": 2.0}, rearm_requested=True))
        second = self.feedback(2)
        self.assertFalse(gate.update(second, arbiter, available, {"pv": 2.0}))
        self.assertTrue(gate.blocked)
        self.assertTrue(gate.update(second, arbiter, available, {"pv": 2.0}, rearm_requested=True))
        self.assertFalse(gate.blocked)
        self.assertEqual(arbiter.owner_caps("network_safety"), {})
        self.assertEqual(arbiter.owner_caps("group_control"), {"pv": 2.0})

    def test_hard_cap_is_never_released_by_network_rearm_and_gaps_reset_confirmation(self):
        arbiter, gate = ActuationArbiter(), NetworkRecoveryInterlock()
        available = {"pv": 4.0}
        arbiter.apply(available, (StationCapIntent("hard_protection", "pv", CapIntentAction.SET_CAP, 1.0),))
        gate.update(self.feedback(snapshot=replace(self.snapshot, quality_valid=False)), arbiter, available, {"pv": 2.0})
        gate.update(self.feedback(1), arbiter, available, {"pv": 1.0})
        self.assertFalse(gate.update(self.feedback(5), arbiter, available, {"pv": 1.0}, rearm_requested=True))
        self.assertTrue(gate.update(self.feedback(6), arbiter, available, {"pv": 1.0}, rearm_requested=True))
        self.assertEqual(arbiter.owner_caps("hard_protection"), {"pv": 1.0})

    def test_old_safe_results_cannot_clear_a_newer_failure(self):
        arbiter, gate = ActuationArbiter(), NetworkRecoveryInterlock()
        available = {"pv": 4.0}
        old1, old2 = self.feedback(1), self.feedback(2)
        gate.update(self.feedback(3, snapshot=replace(self.snapshot, at=self.snapshot.at + timedelta(minutes=3), quality_valid=False)),
                    arbiter, available, {"pv": 2.0})
        for cached in (old1, old2):
            self.assertFalse(gate.update(cached, arbiter, available, {"pv": 2.0}, rearm_requested=True))
        self.assertTrue(gate.blocked)


class MinuteNetworkIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = build_synthetic_case(steps=4)
        cls.mg = cls.case.microgrids[0]
        cls.solved = solve_case_ac_consistent(cls.case, [cls.mg.name], time_limit_seconds=30).optimization

    def track(self, **kwargs):
        return simulate_device_tracking(self.case, self.mg, self.solved,
                                        reverse_flow_probability_15=np.zeros(4), **kwargs)

    def test_minute_control_uses_ac_instead_of_synthetic_safety_flags(self):
        with patch("oilfield_energy.control_contracts.GroupTelemetrySnapshot.synthetic", side_effect=AssertionError("safety stub")), \
             patch("oilfield_energy.model.solve_case", side_effect=AssertionError("redispatch")):
            result = self.track()
        for stage in ("pre_response", "pre_control", "post_control"):
            self.assertEqual(sum(r["stage"] == stage for r in result.network_feedback), len(result.time_minutes))
        self.assertEqual(result.network_invalid_steps, 0)
        finals = [r for r in result.network_feedback if r["stage"] == "post_control"]
        for k, r in enumerate(finals):
            self.assertAlmostEqual(result.pcc_actual_mw[k], r["flow"]["pcc_import_mw"])
            self.assertAlmostEqual(result.qcc_actual_mvar[k], r["flow"]["pcc_reactive_mvar"])
            net = r["inputs"]["p_demand_mw_by_bus"]
            self.assertAlmostEqual(sum(net.values()) + r["flow"]["loss_mw"], result.pcc_actual_mw[k], places=8)

    def test_bad_data_blocks_all_recovery_and_post_action_unknown_is_exported(self):
        def bad(snapshot, stage):
            return replace(snapshot, quality_valid=False)
        result = self.track(network_snapshot_adapter=bad)
        self.assertFalse(result.network_security_passed)
        self.assertEqual(result.network_invalid_steps, len(result.time_minutes))
        self.assertTrue(np.all(result.network_recovery_blocked))
        self.assertTrue(np.all(result.group_requested_restoration_mw == 0))
        self.assertTrue(np.all(np.isnan(result.pcc_actual_mw)))
        self.assertTrue(np.all(np.isnan(result.actual_power_factor)))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_minute_network_outputs(output, {result.name: result})
            lines = (output/"minute_network.jsonl").read_text(encoding="utf-8").splitlines()
            payload = json.loads(lines[-1])
            self.assertFalse(payload["valid"])
            self.assertIsNone(payload["voltage_within_limits"])
            self.assertIsNone(payload["flow"])
            with (output/"minute_network.csv").open(encoding="utf-8-sig") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["pcc_import_mw"], "")

    def test_post_action_failure_latches_next_step_without_rewriting_prior_evidence(self):
        def fault(snapshot, stage):
            # The first current decision is healthy; its post-action check fails.
            return replace(snapshot, quality_valid=False) if stage == "post_control" and ":0:" in snapshot.snapshot_id else snapshot
        result = self.track(network_snapshot_adapter=fault)
        pre = next(r for r in result.network_feedback if r["stage"] == "pre_control")
        post = next(r for r in result.network_feedback if r["stage"] == "post_control")
        self.assertTrue(pre["valid"])
        self.assertFalse(post["valid"])
        self.assertTrue(np.all(result.network_recovery_blocked))
        self.assertEqual(result.network_invalid_steps, 1)
        self.assertFalse(result.network_security_passed)
        self.assertTrue(np.all(result.group_requested_restoration_mw[1:] == 0))

    def test_not_converged_minute_path_cannot_report_safety_pass(self):
        limits = NetworkSecurityLimits(.95, 1.05, 0, 18, .9, max_iterations=1)
        result = self.track(network_limits=limits)
        self.assertFalse(result.network_security_passed)
        self.assertTrue(all(r["status"] == "not_converged" for r in result.network_feedback))
        self.assertTrue(np.all(result.network_recovery_blocked))

    def test_intermediate_failure_is_counted_even_when_final_flow_recovers(self):
        def intermediate(snapshot, stage):
            return replace(snapshot, quality_valid=False) if stage == "plant_response" and ":0:" in snapshot.snapshot_id else snapshot
        result = self.track(network_snapshot_adapter=intermediate)
        self.assertEqual(result.network_invalid_steps, 1)
        self.assertFalse(result.network_security_passed)
        self.assertTrue(np.all(result.network_recovery_blocked))
        final = next(r for r in result.network_feedback if r["stage"] == "post_control")
        self.assertTrue(final["valid"])
        failed = next(r for r in result.network_feedback if r["stage"] == "plant_response")
        self.assertFalse(failed["valid"])

    def test_explicit_rearm_only_transfers_caps_then_group_control_recovers_gradually(self):
        def fault(snapshot, stage):
            return replace(snapshot, quality_valid=False) if ":0:" in snapshot.snapshot_id else snapshot
        result = self.track(network_snapshot_adapter=fault, network_rearm_steps=(2,))
        self.assertTrue(np.all(result.network_recovery_blocked[:2]))
        self.assertFalse(result.network_recovery_blocked[2])
        self.assertEqual(result.group_requested_restoration_mw[2], 0)
        self.assertNotEqual(result.group_control_action[2], "restore")

    def test_post_action_overload_freezes_further_ramp_instead_of_releasing_old_targets(self):
        # A stricter network limit is real input to the solver, not a patched safe flag.
        limited = replace(self.mg, network_model_v2=replace(self.mg.network_model_v2,
                          branches=tuple(replace(b, s_max_mva=.01) for b in self.mg.network_model_v2.branches)))
        result = simulate_device_tracking(self.case, limited, self.solved,
                                         reverse_flow_probability_15=np.zeros(4))
        self.assertFalse(result.network_security_passed)
        self.assertGreater(result.network_violation_steps, 0)
        self.assertTrue(np.all(result.network_recovery_blocked))
        self.assertTrue(np.all(result.group_requested_restoration_mw == 0))
        for power in result.pv_station_actual_mw.values():
            self.assertTrue(np.all(np.diff(power) <= 1e-10))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


from oilfield_energy.actuation_arbiter import ActuationArbiter
from oilfield_energy.control_contracts import CapIntentAction, StationCapIntent
from oilfield_energy.minute_network import (
    MinuteNetworkConfig,
    MinuteNetworkEvaluator,
    NetworkRecoveryInterlock,
)
from oilfield_energy.network_model import NetworkPhaseModel
from oilfield_energy.power_flow_comparison import read_comparison_request


ROOT = Path(__file__).resolve().parents[1]


class MinuteNetworkTests(unittest.TestCase):
    def setUp(self):
        self.request = read_comparison_request(
            ROOT / "tests/fixtures/field_dataset/fixed_device_comparison.json"
        )
        self.snapshot = self.request.snapshot
        self.evaluator = MinuteNetworkEvaluator(
            self.request.network,
            self.request.limits,
            {d.resource_id: d.bus_id for d in self.snapshot.devices},
        )

    def feedback(self, minute=0, *, snapshot=None, evaluator=None):
        now = self.snapshot.at + timedelta(minutes=minute)
        return (evaluator or self.evaluator).evaluate(
            snapshot or replace(self.snapshot, at=now),
            at=now,
            operating_mode_id="normal",
        )

    def test_actual_per_device_mapping_gross_loads_and_pcc_conservation(self):
        with patch(
            "oilfield_energy.model.solve_case", side_effect=AssertionError("redispatch")
        ):
            result = self.feedback()
        self.assertTrue(result.valid)
        self.assertTrue(result.recovery_safe)
        p_net = result.inputs["p_demand_mw_by_bus"]
        self.assertAlmostEqual(p_net["WIND"], -1)
        self.assertAlmostEqual(p_net["MAIN"], 4.7)
        self.assertAlmostEqual(
            result.flow.pcc_import_mw,
            sum(p_net.values()) + result.flow.loss_mw,
            places=8,
        )
        changed = replace(
            self.snapshot,
            devices=tuple(
                replace(d, p_mw=d.p_mw + 1) if d.resource_id == "WT2" else d
                for d in self.snapshot.devices
            ),
        )
        second = self.feedback(snapshot=changed)
        self.assertAlmostEqual(second.inputs["p_demand_mw_by_bus"]["WIND"], -2)
        self.assertEqual(
            result.inputs["snapshot"]["devices"][0],
            second.inputs["snapshot"]["devices"][0],
        )

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
            MinuteNetworkEvaluator(
                self.request.network,
                replace(self.request.limits, max_iterations=1),
                self.evaluator.device_buses,
            ),
            MinuteNetworkEvaluator(
                replace(
                    self.request.network,
                    phase_model=NetworkPhaseModel.THREE_PHASE_UNBALANCED,
                ),
                self.request.limits,
                self.evaluator.device_buses,
            ),
            MinuteNetworkEvaluator(
                replace(
                    self.request.network, branches=self.request.network.branches[:1]
                ),
                self.request.limits,
                self.evaluator.device_buses,
            ),
            MinuteNetworkEvaluator(
                self.request.network,
                self.request.limits,
                self.evaluator.device_buses,
                config=MinuteNetworkConfig(require_field_approval=True),
            ),
            MinuteNetworkEvaluator(
                None, self.request.limits, self.evaluator.device_buses
            ),
        )
        for evaluator, status in zip(
            variants,
            (
                "not_converged",
                "unsupported",
                "islanded",
                "invalid_input",
                "invalid_input",
            ),
        ):
            result = self.feedback(evaluator=evaluator)
            self.assertEqual(result.status, status)
            self.assertFalse(result.recovery_safe)

    def test_voltage_and_overload_are_valid_unsafe_measurements(self):
        model = replace(
            self.request.network,
            branches=tuple(
                replace(b, s_max_mva=0.01) for b in self.request.network.branches
            ),
        )
        evaluator = MinuteNetworkEvaluator(
            model,
            replace(self.request.limits, voltage_max_pu=1.001),
            self.evaluator.device_buses,
        )
        result = self.feedback(evaluator=evaluator)
        self.assertTrue(result.valid)
        self.assertTrue(result.validity.valid)
        self.assertFalse(result.voltage_within_limits)
        self.assertFalse(result.line_capacity_within_limits)
        self.assertFalse(result.recovery_safe)
        self.assertTrue(result.flow.violations)

    def test_interlock_cancels_inflight_recovery_and_requires_fresh_explicit_rearm(
        self,
    ):
        arbiter, gate = ActuationArbiter(), NetworkRecoveryInterlock()
        available = {"pv": 4.0}
        arbiter.apply(
            available,
            (StationCapIntent("group_control", "pv", CapIntentAction.SET_CAP, 3.0),),
        )
        bad = self.feedback(snapshot=replace(self.snapshot, quality_valid=False))
        gate.update(bad, arbiter, available, {"pv": 2.0})
        self.assertTrue(gate.blocked)
        self.assertEqual(arbiter.resolve(available)[0].effective_cap_mw, 2.0)
        # An already issued owner release cannot override the network freeze.
        arbiter.apply(
            available,
            (StationCapIntent("group_control", "pv", CapIntentAction.RELEASE_CAP),),
        )
        self.assertEqual(arbiter.resolve(available)[0].effective_cap_mw, 2.0)
        first = self.feedback(1)
        self.assertFalse(
            gate.update(first, arbiter, available, {"pv": 2.0}, rearm_requested=True)
        )
        # Reusing the same observation with a later evaluation cannot count twice.
        reused = replace(first, evaluated_at=first.evaluated_at + timedelta(seconds=20))
        self.assertFalse(
            gate.update(reused, arbiter, available, {"pv": 2.0}, rearm_requested=True)
        )
        second = self.feedback(2)
        self.assertFalse(gate.update(second, arbiter, available, {"pv": 2.0}))
        self.assertTrue(gate.blocked)
        self.assertTrue(
            gate.update(second, arbiter, available, {"pv": 2.0}, rearm_requested=True)
        )
        self.assertFalse(gate.blocked)
        self.assertEqual(arbiter.owner_caps("network_safety"), {})
        self.assertEqual(arbiter.owner_caps("group_control"), {"pv": 2.0})

    def test_hard_cap_is_never_released_by_network_rearm_and_gaps_reset_confirmation(
        self,
    ):
        arbiter, gate = ActuationArbiter(), NetworkRecoveryInterlock()
        available = {"pv": 4.0}
        arbiter.apply(
            available,
            (StationCapIntent("hard_protection", "pv", CapIntentAction.SET_CAP, 1.0),),
        )
        gate.update(
            self.feedback(snapshot=replace(self.snapshot, quality_valid=False)),
            arbiter,
            available,
            {"pv": 2.0},
        )
        gate.update(self.feedback(1), arbiter, available, {"pv": 1.0})
        self.assertFalse(
            gate.update(
                self.feedback(5), arbiter, available, {"pv": 1.0}, rearm_requested=True
            )
        )
        self.assertTrue(
            gate.update(
                self.feedback(6), arbiter, available, {"pv": 1.0}, rearm_requested=True
            )
        )
        self.assertEqual(arbiter.owner_caps("hard_protection"), {"pv": 1.0})

    def test_old_safe_results_cannot_clear_a_newer_failure(self):
        arbiter, gate = ActuationArbiter(), NetworkRecoveryInterlock()
        available = {"pv": 4.0}
        old1, old2 = self.feedback(1), self.feedback(2)
        gate.update(
            self.feedback(
                3,
                snapshot=replace(
                    self.snapshot,
                    at=self.snapshot.at + timedelta(minutes=3),
                    quality_valid=False,
                ),
            ),
            arbiter,
            available,
            {"pv": 2.0},
        )
        for cached in (old1, old2):
            self.assertFalse(
                gate.update(
                    cached, arbiter, available, {"pv": 2.0}, rearm_requested=True
                )
            )
        self.assertTrue(gate.blocked)


if __name__ == "__main__":
    unittest.main()

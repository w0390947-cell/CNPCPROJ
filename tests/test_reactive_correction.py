from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkBus,
    NetworkDataProvenance,
    NetworkModelV2,
    NetworkOperatingMode,
    SeriesBranch,
    assess_network_model,
)
from oilfield_energy.network_scenarios import NetworkSecurityLimits
from oilfield_energy.reactive_correction import correct_reactive_dispatch
from oilfield_energy.reactive_execution import (
    ReactiveCapability,
    calculate_reactive_capabilities,
)
from oilfield_energy.resource_control_contracts import ResourceSchedule, ResourceType
from tests.legacy_case_fixture import build_synthetic_case


class CorrectiveBoundsTests(unittest.TestCase):
    def setUp(self):
        model = NetworkModelV2(
            "test",
            10.0,
            "A",
            (NetworkBus("A", "A", 10.0), NetworkBus("B", "B", 10.0)),
            (SeriesBranch("AB", "A", "B", NetworkBranchKind.LINE, 0.001, 0.005, 4.0),),
            (NetworkOperatingMode("normal", "test"),),
            "normal",
            NetworkDataProvenance("test", "1", synthetic=True),
        )
        self.network = assess_network_model(model).require_current_solver_ready()
        self.limits = NetworkSecurityLimits(0.95, 1.05, 0.15, 4.0, 0.9)
        self.args = dict(
            p_demand_mw=np.array([0.0, 1.0]),
            q_demand_before_mvar=np.array([0.0, -1.0]),
            resource_buses=["B", "B"],
            current_q=np.zeros(2),
            preferred_targets=np.zeros(2),
            capabilities=(
                ReactiveCapability("disabled", 0.0, 0.0),
                ReactiveCapability("svg", -0.8, 1.2),
            ),
            project_targets=lambda x: x,
            slack_voltage_pu=1.0,
            alpha=1.0,
            ramp_mvar=0.9,
        )

    def test_signed_pf_and_asymmetric_device_interval(self):
        r = correct_reactive_dispatch(self.network, self.limits, **self.args)
        self.assertEqual(r.status, "corrected")
        self.assertEqual(r.targets[0], 0.0)
        self.assertGreaterEqual(r.targets[1], -0.8)
        q = self.args["q_demand_before_mvar"] - np.array(
            [0.0, np.sum(r.predicted_actual)]
        )
        f = backward_forward_sweep_resolved(self.network, self.args["p_demand_mw"], q)
        self.assertGreaterEqual(
            abs(f["pcc_p_mw"]) / np.hypot(f["pcc_p_mw"], f["pcc_q_mvar"]), 0.9
        )
        self.assertGreaterEqual(f["pcc_p_mw"], 0.15)

    def test_no_response_budget_and_nonconvergence_are_not_safe(self):
        r = correct_reactive_dispatch(
            self.network, self.limits, **dict(self.args, ramp_mvar=0.0)
        )
        self.assertEqual(r.status, "held")
        np.testing.assert_array_equal(r.predicted_actual, np.zeros(2))
        r = correct_reactive_dispatch(
            self.network, replace(self.limits, max_iterations=1), **self.args
        )
        self.assertEqual(r.status, "unavailable")

    def test_rejected_group_residual_is_reallocated_to_other_resources(self):
        args = dict(
            self.args,
            capabilities=(
                ReactiveCapability("blocked", -0.8, 1.2),
                ReactiveCapability("available", -0.8, 1.2),
            ),
            preferred_targets=np.array([-0.6, 0.0]),
            project_targets=lambda x: np.array([0.0, x[1]]),
        )
        r = correct_reactive_dispatch(self.network, self.limits, **args)
        self.assertIn(r.status, ("safe", "corrected"))
        self.assertAlmostEqual(np.sum(r.controls), np.sum(r.targets), places=8)
        self.assertAlmostEqual(r.controls[0], 0.0, places=8)
        self.assertLess(r.targets[1], -0.5)

    def test_fixed_slack_and_active_overload_have_explicit_failures(self):
        r = correct_reactive_dispatch(
            self.network, self.limits, **dict(self.args, slack_voltage_pu=0.85)
        )
        self.assertEqual(r.status, "fixed_slack_infeasible")
        for p in (8.0, -8.0):
            r = correct_reactive_dispatch(
                self.network,
                self.limits,
                **dict(self.args, p_demand_mw=np.array([0.0, p])),
            )
            self.assertEqual(r.status, "fixed_active_infeasible")

    def test_nonfinite_optimizer_result_holds_without_emitting_invalid_targets(self):
        with patch(
            "oilfield_energy.reactive_correction.minimize",
            return_value=SimpleNamespace(x=np.array([np.nan])),
        ):
            r = correct_reactive_dispatch(self.network, self.limits, **self.args)
        self.assertEqual(r.status, "held")
        np.testing.assert_array_equal(r.targets, np.zeros(2))

    def test_wind_ratio_and_mva_circle_both_apply(self):
        mg = build_synthetic_case(steps=1).microgrids[0]
        bus = next(iter(mg.wind_available_mw))
        mg = replace(
            mg, wind_q_abs_over_p_max={bus: 0.3}, wind_capacity_mva={bus: 10.0}
        )
        schedule = ResourceSchedule(
            "wind", bus, ResourceType.WIND, np.array([9.9]), np.zeros(1)
        )
        cap = calculate_reactive_capabilities(mg, [schedule], {"wind": 9.9})[0]
        self.assertAlmostEqual(cap.maximum_mvar, np.sqrt(100 - 9.9**2))


if __name__ == "__main__":
    unittest.main()

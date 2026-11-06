import math
import unittest

from oilfield_energy.modules.dispatch.api import (
    calculate_load_change_rate,
    calculate_pv_penetration,
    calculate_risk_index,
    calculate_thresholds,
)
from oilfield_energy.modules.dispatch.contracts import (
    PlanningSecurityConfig,
    SecurityThresholdInput,
)


def _control_input(**overrides: float) -> SecurityThresholdInput:
    values = {
        "time_minutes": 15.0,
        "pcc_power_mw": 1.0,
        "current_net_load_mw": 8.0,
        "previous_net_load_mw": 8.0,
        "maximum_load_mw": 10.0,
        "pv_capacity_mw": 0.0,
        "reverse_flow_probability": 0.0,
        "p_grid_max_mw": 18.0,
    }
    values.update(overrides)
    return SecurityThresholdInput(**values)


class PlanningConfigurationTests(unittest.TestCase):
    def test_invalid_planning_parameters_are_rejected(self) -> None:
        invalid = [
            {"weight_pv_penetration": 0.5},
            {"weight_pv_penetration": "0.3"},
            {
                "weight_load_change_rate": -0.1,
                "weight_pv_penetration": 0.4,
                "weight_reverse_flow_probability": 0.7,
            },
            {"base_safety_margin_ratio": 0.01, "maximum_risk_buffer_ratio": 0.01},
            {"minimum_risk_buffer_ratio": 0.02, "maximum_risk_buffer_ratio": 0.01},
            {
                "minimum_restore_buffer_ratio": 0.04,
                "maximum_restore_buffer_ratio": 0.03,
            },
            {"planning_scenario_count": 0},
            {"planning_scenario_count": True},
            {"risk_probability_horizon_minutes": 1.5},
            {"planning_random_seed": -1},
            {"planning_load_forecast_std_ratio": float("nan")},
        ]
        for parameters in invalid:
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                PlanningSecurityConfig(**parameters)


class PlanningRiskFeatureTests(unittest.TestCase):
    def test_load_change_rate_preserves_sign(self) -> None:
        falling = calculate_load_change_rate(7.2, 8.0, denominator_floor_mw=0.1)
        rising = calculate_load_change_rate(8.8, 8.0, denominator_floor_mw=0.1)
        self.assertAlmostEqual(falling, -0.10)
        self.assertAlmostEqual(rising, 0.10)

    def test_load_change_rate_uses_denominator_floor_near_zero(self) -> None:
        result = calculate_load_change_rate(0.2, 0.0, denominator_floor_mw=1.0)
        self.assertAlmostEqual(result, 0.2)

    def test_pv_penetration_is_capacity_over_maximum_load(self) -> None:
        self.assertAlmostEqual(calculate_pv_penetration(4.2, 9.8), 4.2 / 9.8)

    def test_risk_index_uses_normalized_weighted_features(self) -> None:
        result = calculate_risk_index(
            pv_penetration=0.5,
            load_change_rate=-0.05,
            reverse_flow_probability=0.25,
        )
        self.assertAlmostEqual(result, 0.425)

    def test_risk_features_are_clipped_to_their_reference_levels(self) -> None:
        result = calculate_risk_index(
            pv_penetration=4.0,
            load_change_rate=2.0,
            reverse_flow_probability=1.0,
        )
        self.assertAlmostEqual(result, 1.0)

    def test_invalid_feature_inputs_are_rejected(self) -> None:
        with self.subTest("negative PV capacity"):
            with self.assertRaisesRegex(ValueError, "pv_capacity_mw"):
                calculate_pv_penetration(-1.0, 10.0)
        with self.subTest("zero maximum load"):
            with self.assertRaisesRegex(ValueError, "maximum_load_mw"):
                calculate_pv_penetration(1.0, 0.0)
        with self.subTest("invalid probability"):
            with self.assertRaisesRegex(ValueError, "reverse_flow_probability"):
                calculate_risk_index(0.5, 0.05, 1.01)
        with self.subTest("non-finite load"):
            with self.assertRaisesRegex(ValueError, "current_net_load_mw"):
                calculate_load_change_rate(math.inf, 1.0, denominator_floor_mw=0.1)
        with self.subTest("non-numeric load"):
            with self.assertRaisesRegex(ValueError, "current_net_load_mw"):
                calculate_load_change_rate(  # type: ignore[arg-type]
                    "1.0", 1.0, denominator_floor_mw=0.1
                )
        with self.subTest("non-positive denominator floor"):
            with self.assertRaisesRegex(ValueError, "denominator_floor_mw"):
                calculate_load_change_rate(1.0, 1.0, denominator_floor_mw=0.0)


class PlanningThresholdTests(unittest.TestCase):
    def test_default_low_risk_thresholds_match_synthetic_scale(self) -> None:
        snapshot = calculate_thresholds(_control_input())
        self.assertAlmostEqual(snapshot.risk_index, 0.0)
        self.assertAlmostEqual(snapshot.risk_limit_mw, 0.05)
        self.assertAlmostEqual(snapshot.safety_threshold_mw, 0.15)
        self.assertAlmostEqual(snapshot.restore_threshold_mw, 0.25)
        self.assertFalse(snapshot.was_clipped)

    def test_thresholds_are_strictly_ordered(self) -> None:
        for probability in (0.0, 0.25, 0.5, 0.75, 1.0):
            with self.subTest(probability=probability):
                snapshot = calculate_thresholds(
                    _control_input(
                        current_net_load_mw=7.4,
                        previous_net_load_mw=8.0,
                        pv_capacity_mw=4.2,
                        reverse_flow_probability=probability,
                    )
                )
                self.assertGreater(snapshot.risk_limit_mw, 0.0)
                self.assertLess(snapshot.risk_limit_mw, snapshot.safety_threshold_mw)
                self.assertLess(
                    snapshot.safety_threshold_mw, snapshot.restore_threshold_mw
                )
                self.assertLessEqual(snapshot.restore_threshold_mw, 18.0)

    def test_higher_risk_tightens_curtailment_and_restore_thresholds(self) -> None:
        low = calculate_thresholds(_control_input())
        high = calculate_thresholds(
            _control_input(
                current_net_load_mw=6.0,
                previous_net_load_mw=8.0,
                pv_capacity_mw=10.0,
                reverse_flow_probability=1.0,
            )
        )
        self.assertGreater(high.safety_threshold_mw, low.safety_threshold_mw)
        self.assertGreater(high.risk_limit_mw, low.risk_limit_mw)
        self.assertLess(high.risk_buffer_mw, low.risk_buffer_mw)
        self.assertGreater(high.restore_buffer_mw, low.restore_buffer_mw)
        self.assertGreater(high.restore_threshold_mw, low.restore_threshold_mw)

    def test_threshold_calculation_reports_feature_clipping(self) -> None:
        snapshot = calculate_thresholds(
            _control_input(
                current_net_load_mw=4.0,
                previous_net_load_mw=8.0,
                pv_capacity_mw=20.0,
            )
        )
        self.assertTrue(snapshot.was_clipped)
        self.assertAlmostEqual(snapshot.normalized_pv_penetration, 1.0)
        self.assertAlmostEqual(snapshot.normalized_load_change_rate, 1.0)

    def test_threshold_calculation_is_deterministic(self) -> None:
        control_input = _control_input(
            current_net_load_mw=7.5,
            pv_capacity_mw=4.2,
            reverse_flow_probability=0.35,
        )
        self.assertEqual(
            calculate_thresholds(control_input),
            calculate_thresholds(control_input),
        )

    def test_pcc_capacity_that_cannot_accommodate_thresholds_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "restore threshold exceeds"):
            calculate_thresholds(_control_input(p_grid_max_mw=0.20))

    def test_invalid_runtime_inputs_are_rejected(self) -> None:
        invalid = [
            ("time_minutes", -1.0),
            ("pcc_power_mw", math.nan),
            ("maximum_load_mw", 0.0),
            ("pv_capacity_mw", -0.1),
            ("reverse_flow_probability", -0.01),
            ("p_grid_max_mw", 0.0),
        ]
        for field, value in invalid:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    calculate_thresholds(_control_input(**{field: value}))

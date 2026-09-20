from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.hierarchy_types import GroupControlConfig
from oilfield_energy.planning_security import (
    build_planning_security_trajectories,
    estimate_reverse_flow_probability,
    resolve_security_floors,
)


class PlanningSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = build_synthetic_case(steps=8)

    def test_trajectories_are_deterministic_ordered_and_capacity_feasible(self) -> None:
        first = build_planning_security_trajectories(self.case)
        second = build_planning_security_trajectories(self.case)
        self.assertEqual(set(first), {mg.name for mg in self.case.microgrids})
        for microgrid in self.case.microgrids:
            one = first[microgrid.name]
            two = second[microgrid.name]
            self.assertTrue(np.array_equal(one.reverse_flow_probability, two.reverse_flow_probability))
            self.assertTrue(np.array_equal(one.effective_floor_mw, two.effective_floor_mw))
            self.assertTrue(np.all((one.reverse_flow_probability >= 0.0)))
            self.assertTrue(np.all((one.reverse_flow_probability <= 1.0)))
            self.assertTrue(np.all(one.effective_floor_mw >= one.calculated_safety_threshold_mw))
            self.assertTrue(np.all(one.effective_floor_mw >= microgrid.p_grid_min_mw))
            self.assertTrue(np.all(one.effective_floor_mw <= microgrid.p_grid_max_mw))

    def test_scenario_estimator_uses_forecasts_and_configured_horizon(self) -> None:
        config = GroupControlConfig(
            planning_scenario_count=20,
            planning_load_forecast_std_ratio=0.0,
            planning_pv_forecast_std_ratio=0.0,
            risk_probability_horizon_minutes=15,
        )
        probability = estimate_reverse_flow_probability(
            np.asarray([2.0, 2.0, 2.0]),
            np.asarray([0.0, 0.0, 0.0]),
            np.asarray([1.0, 3.0, 1.0]),
            dt_minutes=15.0,
            config=config,
            random_seed=7,
        )
        self.assertTrue(np.array_equal(probability, np.asarray([0.0, 1.0, 0.0])))

    def test_resolver_rejects_incomplete_or_unsafe_external_floors(self) -> None:
        microgrids = self.case.microgrids[:2]
        valid = build_planning_security_trajectories(
            self.case,
            [microgrid.name for microgrid in microgrids],
        )
        floors = {name: item.effective_floor_mw for name, item in valid.items()}
        resolved = resolve_security_floors(self.case, microgrids, floors)
        self.assertTrue(all(resolved[name] is not floors[name] for name in floors))

        with self.assertRaisesRegex(KeyError, "missing planning security floors"):
            resolve_security_floors(self.case, microgrids, {microgrids[0].name: floors[microgrids[0].name]})
        unsafe = dict(floors)
        unsafe[microgrids[0].name] = np.zeros(len(self.case.time_hours))
        with self.assertRaisesRegex(ValueError, "below the fixed safety minimum"):
            resolve_security_floors(self.case, microgrids, unsafe)

    def test_invalid_planning_scenario_configuration_is_rejected(self) -> None:
        invalid = (
            {"planning_scenario_count": 0},
            {"planning_load_forecast_std_ratio": -0.01},
            {"planning_pv_forecast_std_ratio": -0.01},
            {"planning_random_seed": -1},
            {"planning_random_seed": 1.5},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                GroupControlConfig(**values)


if __name__ == "__main__":
    unittest.main()

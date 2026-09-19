from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.ac_power_flow import validate_ac_dispatch
from oilfield_energy.analysis import validate_result
from oilfield_energy.data import build_synthetic_case
from oilfield_energy.model import solve_case
from oilfield_energy.resource_control_contracts import ResourceSchedule, ResourceType


class OptimizationModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = build_synthetic_case(steps=8)

    def test_synthetic_case_dimensions(self) -> None:
        self.assertEqual(len(self.case.microgrids), 3)
        self.assertEqual(len(self.case.time_hours), 8)
        for mg in self.case.microgrids:
            self.assertEqual(len(mg.buses), 5)
            self.assertEqual(len(mg.lines), 4)
            self.assertTrue(np.all(mg.load_p_mw[mg.buses[1]] >= 0))
            self.assertGreater(mg.maximum_load_mw, 0.0)
            self.assertEqual(set(mg.wind_capacity_mw), set(mg.wind_available_mw))
            self.assertEqual(set(mg.pv_capacity_mw), set(mg.pv_available_mw))

    def test_single_microgrid_is_feasible_and_compliant(self) -> None:
        result = solve_case(
            self.case, ["SC"], storage_enabled=True,
            cluster_coordination=False, time_limit_seconds=30,
        )
        self.assertTrue(result.success, result.message)
        checks = validate_result(self.case, result, ["SC"])
        self.assertTrue(checks["passed"], checks)
        ac_checks = validate_ac_dispatch(self.case, result, ["SC"])
        self.assertTrue(ac_checks["passed"], ac_checks)

    def test_three_microgrid_cluster_is_feasible(self) -> None:
        names = [mg.name for mg in self.case.microgrids]
        result = solve_case(
            self.case, names, storage_enabled=True,
            cluster_coordination=True, time_limit_seconds=30,
        )
        self.assertTrue(result.success, result.message)
        checks = validate_result(self.case, result, names)
        self.assertTrue(checks["passed"], checks)
        self.assertLessEqual(
            float(result.cluster["peak_import_mw"]),
            self.case.cluster_import_limit_mw + 1e-6,
        )

    def test_storage_energy_is_cyclic(self) -> None:
        result = solve_case(
            self.case, ["SC"], storage_enabled=True,
            cluster_coordination=False, time_limit_seconds=30,
        )
        energy = np.asarray(result.microgrids["SC"]["storage_energy_mwh"])
        self.assertLessEqual(
            abs(float(energy[-1] - energy[0])),
            self.case.assumptions.terminal_energy_tolerance_mwh + 1e-6,
        )

    def test_legacy_result_preserves_per_resource_pq_schedules(self) -> None:
        microgrid = self.case.microgrids[0]
        result = solve_case(
            self.case, [microgrid.name], storage_enabled=True,
            cluster_coordination=False, time_limit_seconds=30,
        )
        self.assertTrue(result.success, result.message)
        values = result.microgrids[microgrid.name]
        schedules = values["resource_schedules"]
        self.assertIsInstance(schedules, tuple)
        self.assertEqual(
            len(schedules),
            len(microgrid.wind_available_mw) + len(microgrid.pv_available_mw) + 2,
        )
        self.assertTrue(all(isinstance(item, ResourceSchedule) for item in schedules))
        self.assertEqual(
            len({item.resource_id for item in schedules}),
            len(schedules),
        )
        self.assertTrue(all(not item.active_power_mw.flags.writeable for item in schedules))
        self.assertTrue(all(not item.reactive_power_mvar.flags.writeable for item in schedules))

        wind_p = values["wind_active_by_bus_mw"]
        wind_q = values["wind_reactive_by_bus_mvar"]
        pv_p = values["pv_active_by_bus_mw"]
        pv_q = values["pv_reactive_by_bus_mvar"]
        self.assertEqual(set(wind_p), set(microgrid.wind_available_mw))
        self.assertEqual(set(wind_q), set(microgrid.wind_available_mw))
        self.assertEqual(set(pv_p), set(microgrid.pv_available_mw))
        self.assertEqual(set(pv_q), set(microgrid.pv_available_mw))
        np.testing.assert_allclose(
            np.sum(np.vstack(tuple(wind_p.values())), axis=0),
            values["wind_used_mw"],
        )
        np.testing.assert_allclose(
            np.sum(np.vstack(tuple(wind_q.values())), axis=0),
            values["wind_q_mvar"],
        )
        np.testing.assert_allclose(
            np.sum(np.vstack(tuple(pv_p.values())), axis=0),
            values["pv_used_mw"],
        )
        np.testing.assert_allclose(
            np.sum(np.vstack(tuple(pv_q.values())), axis=0),
            values["pv_q_mvar"],
        )
        np.testing.assert_allclose(
            values["storage_active_mw"],
            np.asarray(values["storage_discharge_mw"])
            - np.asarray(values["storage_charge_mw"]),
        )
        self.assertEqual(
            {item.resource_type for item in schedules},
            {ResourceType.WIND, ResourceType.PV, ResourceType.STORAGE, ResourceType.SVG},
        )

    def test_field_capability_switches_are_enforced_by_optimizer(self) -> None:
        original = self.case.microgrids[0]
        wind_bus = next(iter(original.wind_available_mw))
        pv_bus = next(iter(original.pv_available_mw))
        field_microgrid = replace(
            original,
            wind_q_abs_over_p_max={wind_bus: 0.30},
            pv_reactive_enabled={pv_bus: False},
            storage_reactive_enabled=False,
        )
        field_case = replace(self.case, microgrids=[field_microgrid])
        result = solve_case(
            field_case, [field_microgrid.name], storage_enabled=True,
            cluster_coordination=False, time_limit_seconds=30,
        )
        self.assertTrue(result.success, result.message)
        values = result.microgrids[field_microgrid.name]
        for bus in field_microgrid.wind_available_mw:
            wind_p = np.asarray(values["wind_active_by_bus_mw"][bus])
            wind_q = np.asarray(values["wind_reactive_by_bus_mvar"][bus])
            self.assertTrue(np.all(np.abs(wind_q) <= 0.30 * wind_p + 1e-7))
        self.assertTrue(np.allclose(values["pv_q_mvar"], 0.0, atol=1e-8))
        self.assertTrue(np.allclose(values["storage_q_mvar"], 0.0, atol=1e-8))


if __name__ == "__main__":
    unittest.main()

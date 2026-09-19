from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.resource_control_contracts import (
    ResourceSchedule,
    ResourceType,
    validate_resource_schedules,
)


class ResourceControlContractTests(unittest.TestCase):
    def test_schedule_owns_immutable_finite_copies(self) -> None:
        active = np.array([1.0, 2.0])
        reactive = np.array([0.1, 0.2])
        schedule = ResourceSchedule(
            resource_id="SC:wind:WT_BUS",
            bus_id="WT_BUS",
            resource_type=ResourceType.WIND,
            active_power_mw=active,
            reactive_power_mvar=reactive,
        )
        active[0] = 99.0
        reactive[0] = 99.0
        np.testing.assert_allclose(schedule.active_power_mw, [1.0, 2.0])
        np.testing.assert_allclose(schedule.reactive_power_mvar, [0.1, 0.2])
        self.assertFalse(schedule.active_power_mw.flags.writeable)
        self.assertFalse(schedule.reactive_power_mvar.flags.writeable)

    def test_validation_rejects_duplicate_ids_and_horizon_mismatch(self) -> None:
        first = ResourceSchedule(
            "resource", "BUS_1", ResourceType.PV,
            np.array([1.0, 2.0]), np.array([0.0, 0.0]),
        )
        duplicate = ResourceSchedule(
            "resource", "BUS_2", ResourceType.PV,
            np.array([3.0, 4.0]), np.array([0.0, 0.0]),
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_resource_schedules((first, duplicate), expected_time_steps=2)
        with self.assertRaisesRegex(ValueError, "horizon"):
            validate_resource_schedules((first,), expected_time_steps=3)

    def test_schedule_rejects_nonfinite_or_misaligned_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "same nonzero length"):
            ResourceSchedule(
                "resource", "BUS", ResourceType.SVG,
                np.array([0.0, 0.0]), np.array([0.0]),
            )
        with self.assertRaisesRegex(ValueError, "finite"):
            ResourceSchedule(
                "resource", "BUS", ResourceType.STORAGE,
                np.array([0.0, np.nan]), np.array([0.0, 0.0]),
            )


if __name__ == "__main__":
    unittest.main()

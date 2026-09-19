from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.data import build_synthetic_case
from oilfield_energy.reactive_execution import (
    ReactiveCapability,
    allocate_bounded_reactive_power,
    calculate_reactive_capabilities,
)
from oilfield_energy.resource_control_contracts import ResourceSchedule, ResourceType


def _schedules(microgrid) -> tuple[ResourceSchedule, ...]:
    wind_bus = next(iter(microgrid.wind_available_mw))
    pv_bus = next(iter(microgrid.pv_available_mw))
    zero = np.zeros(1)
    return (
        ResourceSchedule("wind", wind_bus, ResourceType.WIND, zero, zero),
        ResourceSchedule("pv", pv_bus, ResourceType.PV, zero, zero),
        ResourceSchedule(
            "storage", microgrid.storage.bus, ResourceType.STORAGE, zero, zero
        ),
        ResourceSchedule("svg", microgrid.svg_bus, ResourceType.SVG, zero, zero),
    )


class ReactiveExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.microgrid = build_synthetic_case(steps=1).microgrids[0]

    def test_field_authority_and_p_q_envelopes_are_applied_per_resource(self) -> None:
        wind_bus = next(iter(self.microgrid.wind_available_mw))
        pv_bus = next(iter(self.microgrid.pv_available_mw))
        field = replace(
            self.microgrid,
            wind_q_abs_over_p_max={wind_bus: 0.30},
            pv_reactive_enabled={pv_bus: False},
            storage_reactive_enabled=False,
        )
        capabilities = {
            item.resource_id: item
            for item in calculate_reactive_capabilities(
                field,
                _schedules(field),
                {"wind": 4.0, "pv": 2.0, "storage": 1.0, "svg": 0.0},
            )
        }
        self.assertAlmostEqual(capabilities["wind"].minimum_mvar, -1.2)
        self.assertAlmostEqual(capabilities["wind"].maximum_mvar, 1.2)
        self.assertEqual(capabilities["pv"].minimum_mvar, 0.0)
        self.assertEqual(capabilities["pv"].maximum_mvar, 0.0)
        self.assertEqual(capabilities["storage"].minimum_mvar, 0.0)
        self.assertEqual(capabilities["storage"].maximum_mvar, 0.0)
        self.assertEqual(capabilities["svg"].minimum_mvar, field.svg_q_min_mvar)
        self.assertEqual(capabilities["svg"].maximum_mvar, field.svg_q_max_mvar)

    def test_allocation_preserves_identity_and_reports_unserved_q(self) -> None:
        capabilities = (
            ReactiveCapability("wind", -1.0, 1.0),
            ReactiveCapability("svg", -2.0, 2.0),
        )
        feasible = allocate_bounded_reactive_power(
            2.0,
            {"wind": 0.2, "svg": 0.3},
            capabilities,
        )
        self.assertAlmostEqual(feasible.achieved_total_mvar, 2.0)
        self.assertAlmostEqual(feasible.unserved_mvar, 0.0)
        self.assertEqual(set(feasible.target_by_resource), {"wind", "svg"})
        self.assertLessEqual(feasible.target_by_resource["wind"], 1.0)
        self.assertLessEqual(feasible.target_by_resource["svg"], 2.0)

        infeasible = allocate_bounded_reactive_power(
            5.0,
            {"wind": 0.0, "svg": 0.0},
            capabilities,
        )
        self.assertAlmostEqual(infeasible.achieved_total_mvar, 3.0)
        self.assertAlmostEqual(infeasible.unserved_mvar, 2.0)

    def test_capability_snapshot_must_cover_every_resource_exactly(self) -> None:
        with self.assertRaisesRegex(ValueError, "every resource exactly"):
            calculate_reactive_capabilities(
                self.microgrid,
                _schedules(self.microgrid),
                {"wind": 1.0},
            )


if __name__ == "__main__":
    unittest.main()

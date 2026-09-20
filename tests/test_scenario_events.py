from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.scenario_events import (
    EventTimeAxis,
    EventType,
    ScenarioEvent,
    apply_physical_events,
)
from oilfield_energy.service import ScenarioType, SimulationRequest, run_simulation


class ScenarioEventTests(unittest.TestCase):
    def test_pv_surge_is_bounded_and_does_not_mutate_source_case(self) -> None:
        source = build_synthetic_case(steps=24)
        original = source.microgrids[0].pv_available_mw["SC_PV"].copy()
        event = ScenarioEvent(
            event_id="pv-noon",
            event_type=EventType.PV_SURGE,
            target="SC",
            time_axis=EventTimeAxis.CLOCK_MINUTE,
            start=600,
            end=900,
            magnitude=1.5,
            label="中午光伏大发",
        )
        changed = apply_physical_events(source, [event])
        updated = changed.microgrids[0].pv_available_mw["SC_PV"]
        mask = (source.time_hours * 60 >= 600) & (source.time_hours * 60 < 900)
        self.assertTrue(np.all(updated[mask] >= original[mask]))
        self.assertTrue(np.any(updated[mask] > original[mask]))
        self.assertLessEqual(float(np.max(updated)), 4.2)
        self.assertTrue(
            np.array_equal(source.microgrids[0].pv_available_mw["SC_PV"], original)
        )

    def test_wind_surge_is_bounded_and_does_not_mutate_source_case(self) -> None:
        source = build_synthetic_case(steps=24)
        original = source.microgrids[0].wind_available_mw["SC_WIND"].copy()
        event = ScenarioEvent(
            event_id="wind-noon",
            event_type=EventType.WIND_SURGE,
            target="SC",
            time_axis=EventTimeAxis.CLOCK_MINUTE,
            start=600,
            end=900,
            magnitude=1.5,
            label="中午风电大发",
        )
        changed = apply_physical_events(source, [event])
        updated = changed.microgrids[0].wind_available_mw["SC_WIND"]
        mask = (source.time_hours * 60 >= 600) & (source.time_hours * 60 < 900)
        self.assertTrue(np.all(updated[mask] >= original[mask]))
        self.assertTrue(np.any(updated[mask] > original[mask]))
        self.assertLessEqual(float(np.max(updated)), 10.0)
        self.assertTrue(
            np.array_equal(source.microgrids[0].wind_available_mw["SC_WIND"], original)
        )

    def test_load_drop_scales_active_and_reactive_power_together(self) -> None:
        source = build_synthetic_case(steps=8)
        event = ScenarioEvent(
            event_id="load-drop",
            event_type=EventType.LOAD_DROP,
            target="YA_B",
            time_axis=EventTimeAxis.CLOCK_MINUTE,
            start=0,
            end=360,
            magnitude=0.7,
            label="负荷骤降",
        )
        changed = apply_physical_events(source, [event])
        before = source.microgrids[1]
        after = changed.microgrids[1]
        self.assertAlmostEqual(
            after.load_p_mw["YA_B_MAIN"][0], before.load_p_mw["YA_B_MAIN"][0] * 0.7
        )
        self.assertAlmostEqual(
            after.load_q_mvar["YA_B_MAIN"][0], before.load_q_mvar["YA_B_MAIN"][0] * 0.7
        )

    def test_communication_event_overrides_default_outage_window(self) -> None:
        request = SimulationRequest(
            name="事件驱动失联",
            scenario_type=ScenarioType.COMMUNICATION_FAULT,
            steps=4,
            events=[
                ScenarioEvent(
                    event_id="ya-b-outage",
                    event_type=EventType.COMMUNICATION_OUTAGE,
                    target="YA_B",
                    time_axis=EventTimeAxis.COORDINATION_ITERATION,
                    start=3,
                    end=8,
                    label="YA_B 失联",
                ),
                ScenarioEvent(
                    event_id="packet-loss",
                    event_type=EventType.COMMUNICATION_PACKET_LOSS,
                    target="YA_B",
                    time_axis=EventTimeAxis.COORDINATION_ITERATION,
                    start=4,
                    end=9,
                    magnitude=0.35,
                    label="区间丢包",
                ),
            ],
        )
        result = run_simulation(request)
        self.assertEqual(result.communication.outage_start_iteration, 3)
        self.assertEqual(result.communication.outage_end_iteration, 8)
        self.assertEqual(result.communication.loss_start_iteration, 4)
        self.assertEqual(result.communication.loss_end_iteration, 9)
        self.assertAlmostEqual(result.communication.loss_probability, 0.35)
        self.assertGreater(result.communication.outage_dropped, 0)

    def test_event_validation_rejects_wrong_time_axis_and_duplicate_ids(self) -> None:
        with self.assertRaises(ValueError):
            ScenarioEvent(
                event_id="bad",
                event_type=EventType.PV_SURGE,
                target="SC",
                time_axis=EventTimeAxis.COORDINATION_ITERATION,
                start=1,
                end=2,
                magnitude=1.2,
                label="错误事件",
            )
        event = ScenarioEvent(
            event_id="duplicate",
            event_type=EventType.PV_SURGE,
            target="SC",
            time_axis=EventTimeAxis.CLOCK_MINUTE,
            start=600,
            end=720,
            magnitude=1.2,
            label="重复事件",
        )
        with self.assertRaises(ValueError):
            SimulationRequest(events=[event, event])


if __name__ == "__main__":
    unittest.main()

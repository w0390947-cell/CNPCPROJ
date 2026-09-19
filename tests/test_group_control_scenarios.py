from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.group_control_scenarios import (
    run_group_control_scenarios,
    write_group_control_scenario_outputs,
)


class GroupControlScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.results = run_group_control_scenarios()

    def test_all_required_scenarios_pass_with_stable_names(self) -> None:
        self.assertEqual(
            tuple(self.results),
            (
                "normal_operation",
                "emergency_curtailment",
                "consecutive_three_trigger",
                "three_in_five_debounce",
                "secondary_curtailment",
                "successful_progressive_recovery",
                "recovery_response_failure",
                "communication_failure_and_reentry",
            ),
        )
        self.assertTrue(all(result.passed for result in self.results.values()))
        self.assertTrue(all(not result.failed_checks for result in self.results.values()))

    def test_trigger_recovery_and_communication_evidence_is_present(self) -> None:
        emergency = self.results["emergency_curtailment"]
        self.assertEqual(emergency.events[-1].reason, "emergency_load_change_rate")

        debounce = self.results["three_in_five_debounce"]
        self.assertEqual(debounce.events[-1].reason, "frequency_risk_limit")

        recovery = self.results["successful_progressive_recovery"]
        restoration_events = [
            event for event in recovery.events
            if event.event_type == "restoration_command"
        ]
        self.assertEqual(len(restoration_events), 10)
        self.assertTrue(any(event.reason == "recovery_completed" for event in recovery.events))

        failure = self.results["recovery_response_failure"]
        self.assertEqual(failure.events[-1].event_type, "recovery_abort")
        self.assertEqual(failure.events[-1].reason, "restoration_response_mismatch")

        communication = self.results["communication_failure_and_reentry"]
        self.assertTrue(any(
            event.reason == "recovery_command_not_acknowledged"
            for event in communication.events
        ))
        self.assertEqual(communication.records[-1].decision.action.value, "restore")

    def test_scenarios_are_deterministic(self) -> None:
        repeated = run_group_control_scenarios()
        self.assertEqual(self.results, repeated)

    def test_scenario_outputs_are_complete(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory)
            write_group_control_scenario_outputs(output, self.results)
            expected = {
                "summary.json",
                "scenario_timeseries.csv",
                "scenario_events.csv",
                "scenario_overview.png",
            }
            self.assertTrue(all((output / name).is_file() for name in expected))
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["all_passed"])
            self.assertEqual(summary["scenario_count"], 8)

            with (output / "scenario_timeseries.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                len(rows),
                sum(len(result.records) for result in self.results.values()),
            )
            self.assertTrue({
                "scenario", "pcc_power_mw", "state", "action", "reason",
                "recovery_evaluation_passed",
            }.issubset(rows[0]))

            image = (output / "scenario_overview.png").read_bytes()
            self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(image), 10_000)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import csv
import json
import sys
import unittest
from math import acos, tan
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.admm import run_admm_coordination
from oilfield_energy.data import build_synthetic_case
from oilfield_energy.hierarchical import run_hierarchical_control
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig
from oilfield_energy.hierarchy_reporting import write_hierarchical_outputs


class HierarchicalControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = build_synthetic_case(steps=8)
        cls.names = [mg.name for mg in cls.case.microgrids]
        cls.result = run_hierarchical_control(
            cls.case,
            cls.names,
            admm_config=ADMMConfig(max_iterations=120),
            time_limit_seconds=60,
        )

    def test_admm_converges_and_cluster_references_are_feasible(self) -> None:
        admm = self.result.admm
        self.assertTrue(admm.converged, admm.stop_reason)
        self.assertGreaterEqual(admm.history[-1].convergence_streak, 3)
        self.assertLessEqual(
            float(np.max(admm.aggregate_import_mw)),
            self.case.cluster_import_limit_mw + 1e-6,
        )
        q_limit = tan(acos(0.95)) * admm.aggregate_import_mw
        self.assertTrue(np.all(np.abs(admm.aggregate_q_mvar) <= q_limit + 1e-6))

    def test_independent_misocp_realization_and_device_safety(self) -> None:
        comparison = self.result.comparison
        self.assertLess(float(comparison["p_reference_realization_rmse_mw"]), 1e-3)
        self.assertLess(float(comparison["q_reference_realization_rmse_mvar"]), 5e-4)
        self.assertTrue(comparison["cluster_import_limit_passed"])
        self.assertTrue(comparison["local_ac_validation_passed"])
        self.assertTrue(comparison["intraday_ac_validation_passed"])
        self.assertTrue(comparison["device_safety_passed"])
        self.assertTrue(comparison["reactive_execution_passed"])
        self.assertLessEqual(
            float(comparison["maximum_reactive_unserved_mvar"]), 1e-8
        )
        self.assertTrue(comparison["centralized_ac_consistency_passed"])
        self.assertTrue(comparison["all_regional_ac_consistency_passed"])
        self.assertTrue(comparison["all_intraday_ac_consistency_passed"])
        self.assertGreater(float(comparison["legacy_loss_underestimate_percent"]), 0.0)
        self.assertLess(float(comparison["misocp_loss_relative_error_percent"]), 1e-4)
        for name in self.names:
            self.assertEqual(set(self.result.local_milp_results[name].microgrids), {name})

    def test_device_layer_separates_wind_pv_and_group_control(self) -> None:
        valid_states = {
            "normal",
            "prepared",
            "risk_observing",
            "curtailing",
            "curtailed_hold",
            "restore_wait",
            "restoring",
            "recovery_aborted",
            "recovery_inhibit",
            "output_block",
            "hard_override",
        }
        for item in self.result.tracking.values():
            length = len(item.time_minutes)
            series = (
                item.wind_command_mw,
                item.wind_available_mw,
                item.wind_actual_mw,
                item.pv_plan_mw,
                item.pv_available_mw,
                item.pv_control_target_mw,
                item.pv_actual_mw,
                item.storage_actual_mw,
                item.wind_reactive_actual_mvar,
                item.pv_reactive_actual_mvar,
                item.storage_reactive_actual_mvar,
                item.svg_reactive_actual_mvar,
                item.reactive_dispatch_unserved_mvar,
                item.shancheng_reactive_command_accepted,
                item.reactive_execution_known,
                item.hard_wind_storage_target_mw,
                item.hard_safety_unserved_mw,
                item.hard_wind_storage_command_accepted,
                item.group_control_state,
                item.group_control_action,
                item.group_risk_limit_mw,
                item.group_safety_threshold_mw,
                item.group_restore_threshold_mw,
                item.group_current_net_load_mw,
                item.group_load_change_rate,
                item.group_pv_penetration,
                item.group_reverse_flow_probability,
                item.group_observed_reverse_flow_probability,
                item.group_reverse_flow_probability_valid,
                item.group_risk_index,
                item.group_required_curtailment_mw,
                item.group_requested_curtailment_mw,
                item.group_unserved_curtailment_mw,
                item.group_requested_restoration_mw,
                item.group_achieved_restoration_mw,
                item.group_remaining_curtailment_mw,
                item.group_recovery_dwell_remaining_minutes,
                item.group_recovery_evaluation_passed,
                item.group_restoration_response_error_mw,
                item.measured_pv_curtailment_mw,
                item.pcc_before_local_safety_mw,
                item.local_reverse_flow_reduction_mw,
                item.group_hard_override_active,
                item.local_power_factor_adjustment_mvar,
                item.actual_power_factor,
                item.storage_energy_mwh,
                item.storage_soc_within_limits,
            )
            self.assertTrue(all(len(values) == length for values in series))
            self.assertTrue(np.all(item.wind_actual_mw >= -1e-9))
            self.assertTrue(np.all(item.pv_actual_mw >= -1e-9))
            self.assertTrue(np.all(item.pv_control_target_mw >= -1e-9))
            self.assertTrue(np.all(item.pv_control_target_mw <= item.pv_available_mw + 1e-9))
            self.assertEqual(
                set(item.pv_station_actual_mw),
                set(item.pv_station_control_target_mw),
            )
            self.assertTrue(np.allclose(
                np.sum(np.vstack(list(item.pv_station_actual_mw.values())), axis=0),
                item.pv_actual_mw,
            ))
            self.assertTrue(np.allclose(
                np.sum(
                    np.vstack(list(item.pv_station_control_target_mw.values())),
                    axis=0,
                ),
                item.pv_control_target_mw,
            ))
            planned_resources = self.result.local_milp_results[
                item.name
            ].microgrids[item.name]["resource_schedules"]
            resource_ids = {schedule.resource_id for schedule in planned_resources}
            self.assertEqual(set(item.reactive_resource_plan_mvar), resource_ids)
            self.assertEqual(set(item.reactive_resource_target_mvar), resource_ids)
            self.assertEqual(set(item.reactive_resource_actual_mvar), resource_ids)
            for values in item.reactive_resource_actual_mvar.values():
                self.assertEqual(len(values), length)
                self.assertTrue(np.all(np.isfinite(values)))
            np.testing.assert_allclose(
                np.sum(
                    np.vstack(tuple(item.reactive_resource_actual_mvar.values())),
                    axis=0,
                ),
                item.wind_reactive_actual_mvar
                + item.pv_reactive_actual_mvar
                + item.storage_reactive_actual_mvar
                + item.svg_reactive_actual_mvar,
            )
            self.assertTrue(np.all(item.reactive_dispatch_unserved_mvar <= 1e-8))
            self.assertTrue(np.all(item.shancheng_reactive_command_accepted))
            self.assertTrue(np.all(item.reactive_execution_known))
            self.assertTrue(np.all(
                np.abs(item.wind_reactive_actual_mvar)
                <= 0.30 * item.wind_actual_mw + 1e-8
            ))
            self.assertTrue(set(item.group_control_state).issubset(valid_states))
            self.assertTrue(np.all(
                item.group_risk_limit_mw < item.group_safety_threshold_mw
            ))
            self.assertTrue(np.all(
                item.group_safety_threshold_mw < item.group_restore_threshold_mw
            ))
            self.assertTrue(np.all((item.group_risk_index >= 0.0)))
            self.assertTrue(np.all((item.group_risk_index <= 1.0)))
            self.assertTrue(np.all((item.group_reverse_flow_probability >= 0.0)))
            self.assertTrue(np.all((item.group_reverse_flow_probability <= 1.0)))
            self.assertTrue(np.all(item.group_reverse_flow_probability_valid))
            self.assertTrue(np.all(np.isfinite(
                item.group_observed_reverse_flow_probability
            )))
            self.assertEqual(
                item.safety_interventions,
                item.local_reverse_flow_interventions
                + item.local_power_factor_interventions,
            )
            self.assertEqual(
                item.group_curtailment_commands,
                sum(
                    event.event_type == "curtailment_command"
                    for event in item.group_control_events
                ),
            )
            self.assertEqual(
                item.group_restoration_commands,
                sum(
                    event.event_type == "restoration_command"
                    for event in item.group_control_events
                ),
            )
        self.assertEqual(
            sum(item.group_curtailment_commands for item in self.result.tracking.values()),
            int(self.result.comparison["group_control_curtailment_commands"]),
        )
        self.assertEqual(
            sum(item.group_restoration_commands for item in self.result.tracking.values()),
            int(self.result.comparison["group_control_restoration_commands"]),
        )

    def test_group_control_reporting_artifacts_are_complete(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory)
            write_hierarchical_outputs(
                output,
                self.case,
                self.names,
                self.result,
                configuration={"test": True},
            )
            expected = {
                "group_control_timeseries.csv",
                "group_control_events.csv",
                "group_control_thresholds.png",
                "pv_curtailment_and_recovery.png",
            }
            self.assertTrue(all((output / name).is_file() for name in expected))

            with (output / "group_control_timeseries.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                len(rows),
                sum(len(item.time_minutes) for item in self.result.tracking.values()),
            )
            self.assertTrue({
                "risk_index", "state", "action", "measured_curtailment_mw",
                "recovery_evaluation_passed", "local_reverse_flow_action",
                "hard_wind_storage_target_mw",
                "hard_wind_storage_command_accepted",
                "hard_safety_unserved_mw",
                "wind_q_actual_mvar", "pv_q_actual_mvar",
                "storage_q_actual_mvar", "svg_q_actual_mvar",
                "reactive_unserved_mvar", "reactive_command_accepted",
                "reactive_execution_known",
            }.issubset(rows[0]))

            with (output / "group_control_events.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                event_rows = list(csv.DictReader(handle))
            self.assertEqual(
                len(event_rows),
                sum(len(item.group_control_events) for item in self.result.tracking.values()),
            )

            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(set(summary["group_control_metrics"]), set(self.names))
            for image_name in (
                "group_control_thresholds.png",
                "pv_curtailment_and_recovery.png",
            ):
                image = (output / image_name).read_bytes()
                self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertGreater(len(image), 10_000)

    def test_planning_security_floor_is_shared_by_all_optimization_layers(self) -> None:
        self.assertTrue(self.result.comparison["day_ahead_planning_security_passed"])
        self.assertTrue(self.result.comparison["intraday_planning_security_passed"])
        for name in self.names:
            day_floor = self.result.day_ahead_security[name].effective_floor_mw
            intraday_floor = self.result.intraday_security[name].effective_floor_mw
            central = self.result.centralized_reference.microgrids[name]
            regional = self.result.local_milp_results[name].microgrids[name]
            intraday = self.result.intraday_milp_results[name].microgrids[name]

            self.assertTrue(np.all(self.result.admm.p_references_mw[name] >= day_floor - 1e-6))
            self.assertTrue(np.allclose(central["p_grid_security_floor_mw"], day_floor))
            self.assertTrue(np.allclose(regional["p_grid_security_floor_mw"], day_floor))
            self.assertTrue(np.allclose(intraday["p_grid_security_floor_mw"], intraday_floor))
            self.assertTrue(
                self.result.centralized_ac_consistency.ac_validation["microgrids"][name][
                    "planning_security_floor_compliant"
                ]
            )

    def test_delay_loss_outage_fallback_and_recovery(self) -> None:
        fault = run_admm_coordination(
            self.case,
            self.names,
            admm_config=ADMMConfig(max_iterations=220),
            communication_config=CommunicationConfig(
                loss_probability=0.05,
                min_delay_iterations=0,
                max_delay_iterations=2,
                stale_limit_iterations=3,
                outage_region="YA_B",
                outage_start_iteration=12,
                outage_end_iteration=22,
            ),
        )
        self.assertTrue(fault.converged, fault.stop_reason)
        self.assertGreater(fault.communication.dropped, 0)
        self.assertGreater(fault.communication.delayed, 0)
        self.assertGreater(fault.communication.outage_dropped, 0)
        self.assertGreater(fault.communication.stale_uses, 0)
        self.assertGreater(fault.communication.fallback_uses, 0)
        self.assertLessEqual(
            float(np.max(fault.aggregate_import_mw)),
            self.case.cluster_import_limit_mw + 1e-6,
        )


if __name__ == "__main__":
    unittest.main()

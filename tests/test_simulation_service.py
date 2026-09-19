from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.service import (
    ScenarioType,
    SimulationRequest,
    SimulationStage,
    SolverFormulation,
    SolverOptions,
    run_simulation,
)


class SimulationServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.progress = []
        cls.request = SimulationRequest(
            name="8点单微网服务基线",
            scenario_type=ScenarioType.SINGLE_MICROGRID,
            region="SC",
            steps=8,
            solver=SolverOptions(
                formulation=SolverFormulation.MISOCP,
                time_limit_seconds=30,
            ),
        )
        cls.result = run_simulation(cls.request, cls.progress.append)

    def test_result_is_json_serializable_and_versioned(self) -> None:
        payload = self.result.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.assertIn("参数化模拟数据", encoded)
        self.assertEqual(payload["metadata"]["schema_version"], "1.3.0")
        self.assertEqual(payload["metadata"]["steps"], 8)

    def test_true_solver_result_contains_complete_display_contract(self) -> None:
        self.assertTrue(self.result.executive_summary.overall_passed)
        self.assertEqual(len(self.result.timeseries), 8)
        self.assertEqual(len(self.result.topology_nodes), 5)
        self.assertEqual(len(self.result.topology_edges), 4)
        self.assertTrue(all(item.passed for item in self.result.validation_items))
        self.assertIn("SCIP status", self.result.solver["message"])

    def test_timeseries_units_and_security_relationships_are_consistent(self) -> None:
        for point in self.result.timeseries:
            self.assertGreaterEqual(
                point.p_grid_optimized_mw + 2e-5,
                point.p_grid_security_floor_mw,
            )
            self.assertAlmostEqual(
                point.p_grid_optimized_mw - point.p_grid_security_floor_mw,
                point.p_grid_security_headroom_mw,
                places=6,
            )
            self.assertLessEqual(point.maximum_line_loading_pu, 1.0 + 2e-5)

    def test_progress_callback_reports_stable_ordered_stages(self) -> None:
        self.assertEqual(
            [item.stage for item in self.progress],
            [
                SimulationStage.PREPARING_CASE,
                SimulationStage.SOLVING_BASELINE,
                SimulationStage.SOLVING_OPTIMIZED,
                SimulationStage.VALIDATING,
                SimulationStage.SERIALIZING,
                SimulationStage.SUCCEEDED,
            ],
        )
        self.assertEqual([item.sequence for item in self.progress], list(range(1, 7)))

    def test_contract_rejects_reversed_communication_outage(self) -> None:
        with self.assertRaises(ValueError):
            SimulationRequest(
                scenario_type=ScenarioType.COMMUNICATION_FAULT,
                communication_outage_start_iteration=20,
                communication_outage_end_iteration=10,
            )


class MultiScenarioServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = run_simulation(SimulationRequest(
            name="4点集群协调",
            scenario_type=ScenarioType.CLUSTER_COORDINATION,
            steps=4,
            admm_max_iterations=220,
        ))
        cls.fault = run_simulation(SimulationRequest(
            name="4点通信故障",
            scenario_type=ScenarioType.COMMUNICATION_FAULT,
            steps=4,
            admm_max_iterations=220,
            communication_outage_start_iteration=3,
            communication_outage_end_iteration=8,
        ))
        cls.group = run_simulation(SimulationRequest(
            name="群控状态机回归",
            scenario_type=ScenarioType.GROUP_CONTROL,
            steps=4,
        ))

    def test_cluster_coordination_contains_real_admm_and_three_regions(self) -> None:
        self.assertTrue(self.cluster.executive_summary.overall_passed)
        self.assertEqual(len(self.cluster.topology_nodes), 15)
        self.assertEqual(len(self.cluster.cluster_timeseries), 4)
        self.assertGreater(len(self.cluster.admm_history), 3)
        self.assertTrue(self.cluster.solver["success"])
        self.assertEqual(self.cluster.communication.dropped, 0)

    def test_communication_fault_records_outage_and_recovery(self) -> None:
        self.assertTrue(self.fault.executive_summary.overall_passed)
        self.assertGreater(self.fault.communication.dropped, 0)
        self.assertGreater(self.fault.communication.outage_dropped, 0)
        self.assertGreater(self.fault.communication.fallback_uses, 0)
        self.assertEqual(self.fault.communication.outage_region, "YA_B")
        self.assertEqual(
            self.fault.admm_history[-1].convergence_streak,
            3,
        )

    def test_group_control_contains_auditable_state_machine_trajectories(self) -> None:
        self.assertTrue(self.group.executive_summary.overall_passed)
        self.assertEqual(self.group.group_control.scenario_count, 8)
        self.assertTrue(self.group.group_control.all_passed)
        self.assertGreater(len(self.group.group_control.records), 8)
        self.assertGreater(len(self.group.group_control.events), 0)
        actions = {record.action for record in self.group.group_control.records}
        self.assertTrue({"curtail", "restore", "abort_recovery"}.issubset(actions))

    def test_all_scenario_results_are_strict_json(self) -> None:
        for result in (self.cluster, self.fault, self.group):
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)

    def test_cluster_timeseries_has_real_aggregate_balance_inputs(self) -> None:
        for point in self.cluster.cluster_timeseries:
            self.assertGreater(point.aggregate_load_mw, 0.0)
            self.assertGreaterEqual(point.aggregate_renewable_mw, 0.0)
            self.assertAlmostEqual(
                point.aggregate_import_mw,
                sum(point.regional_import_mw.values()),
                places=6,
            )


if __name__ == "__main__":
    unittest.main()

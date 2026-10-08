"""Current cluster evidence; retired DeviceTrackingResult collectors are not forged."""

import unittest

import numpy as np

from oilfield_energy import service
from oilfield_energy.admm import run_admm_coordination
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig
from oilfield_energy.workflows.cluster_execution.contracts import ClusterExecution
from tests.support.reserve_case import stationary_reserve_case


class ClusterRollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = stationary_reserve_case()
        cls.names = [m.name for m in cls.case.microgrids]
        cls.execution = service.run_simulation(
            service.SimulationRequest(
                scenario_type="cluster_coordination", steps=4, storage_enabled=False
            ),
            input_case=cls.case,
        ).cluster_execution
        assert cls.execution is not None and cls.execution.safety is not None

    def test_rolling_evidence_binds_each_actual_response_and_continuation(self):
        e = self.execution
        self.assertEqual(e.algorithm_version, "cluster-rolling-v6")
        self.assertEqual(e.policy.cluster_transition_version, "cluster-continuation-v1")
        self.assertEqual(len(e.safety.steps), 60)
        self.assertTrue(
            all(
                u.continuation_available and u.actual_end_energy_mwh
                for u in e.rolling_updates
            )
        )
        for a, b in zip(e.safety.steps, e.safety.steps[1:]):
            self.assertEqual(a.after, b.before)
            self.assertEqual(a.minute + 1, b.minute)
        # V1 capacity-only receipts cannot be relabeled as the new full proof.
        self.assertFalse(e.cluster_transitions)

    def test_serialization_retains_joint_proof_and_actual_resource_channels(self):
        replayed = ClusterExecution.model_validate_json(
            self.execution.model_dump_json()
        )
        self.assertEqual(replayed, self.execution)
        for i, row in enumerate(replayed.stages[2].regions):
            self.assertEqual(set(row.resource_p_mw), set(row.resource_q_mvar))
            resources = replayed.safety.domain.regions[i].devices.resources
            for j, resource in enumerate(resources):
                self.assertEqual(
                    row.resource_p_mw[resource.resource_id],
                    tuple(s.after[i].p_mw[j] for s in replayed.safety.steps),
                )
                self.assertEqual(
                    row.resource_q_mvar[resource.resource_id],
                    tuple(s.after[i].q_mvar[j] for s in replayed.safety.steps),
                )

    def test_wind_pv_storage_svg_identities_and_disabled_storage_are_preserved(self):
        for region in self.execution.safety.domain.regions:
            self.assertEqual(
                {r.kind for r in region.devices.resources},
                {"wind", "pv", "storage", "svg"},
            )
        for row in self.execution.stages[2].regions:
            self.assertTrue(all(p == 0 for p in row.storage_power_mw))
            self.assertLess(np.ptp(row.storage_energy_mwh), 1e-12)
        self.assertFalse(self.execution.reserve_observations)

    def test_delay_loss_outage_fallback_and_recovery(self) -> None:
        fault_case = build_synthetic_case(8)
        fault = run_admm_coordination(
            fault_case,
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
            fault_case.cluster_import_limit_mw + 1e-6,
        )

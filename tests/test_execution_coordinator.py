from __future__ import annotations

import unittest
from dataclasses import replace


from tests.support.protection import protection_telemetry as _telemetry
from oilfield_energy.control_contracts import PccSafetyConstraint
from oilfield_energy.execution_coordinator import ShanchengSafetyCoordinator


class ShanchengSafetyCoordinatorTests(unittest.TestCase):
    def test_post_pv_deficit_is_routed_to_storage_before_wind(self) -> None:
        coordinator = ShanchengSafetyCoordinator(source_epoch="test-epoch")
        result, decision = coordinator.allocate(
            _telemetry(),
            PccSafetyConstraint("hard", 0.15, "decision-1"),
            pcc_after_confirmed_pv_mw=-0.85,
            confirmed_pv_reduction_mw=0.5,
        )

        self.assertTrue(result.command_accepted)
        self.assertAlmostEqual(result.requested_wind_storage_reduction_mw, 1.0)
        self.assertAlmostEqual(result.wind_storage_target_mw, 7.0)
        self.assertIsNotNone(decision)
        self.assertAlmostEqual(decision.setpoints.wind[0].active_power_mw, 8.0)
        self.assertAlmostEqual(decision.setpoints.storage_active_power_mw, -1.0)

    def test_larger_deficit_uses_storage_then_wind_and_latches_target(self) -> None:
        coordinator = ShanchengSafetyCoordinator(source_epoch="test-epoch")
        result, decision = coordinator.allocate(
            _telemetry(),
            PccSafetyConstraint("hard", 0.15, "decision-1"),
            pcc_after_confirmed_pv_mw=-3.35,
            confirmed_pv_reduction_mw=0.0,
        )

        self.assertTrue(result.command_accepted)
        self.assertAlmostEqual(result.wind_storage_target_mw, 4.5)
        self.assertAlmostEqual(decision.setpoints.storage_active_power_mw, -2.5)
        self.assertAlmostEqual(decision.setpoints.wind[0].active_power_mw, 7.0)
        self.assertAlmostEqual(coordinator.active_target_cap_mw, 4.5)

    def test_invalid_device_measurement_is_unserved_not_subtracted(self) -> None:
        coordinator = ShanchengSafetyCoordinator(source_epoch="test-epoch")
        result, decision = coordinator.allocate(
            _telemetry(active_valid=False),
            PccSafetyConstraint("hard", 0.15, "decision-1"),
            pcc_after_confirmed_pv_mw=-0.35,
            confirmed_pv_reduction_mw=0.0,
        )

        self.assertFalse(result.command_accepted)
        self.assertAlmostEqual(result.unserved_reduction_mw, 0.5)
        self.assertIsNone(result.wind_storage_target_mw)
        self.assertIsNotNone(decision)
        self.assertTrue(
            all(item.active_power_mw is None for item in decision.setpoints.wind)
        )

    def test_existing_hard_target_cannot_be_relaxed_by_another_curtailment(
        self,
    ) -> None:
        coordinator = ShanchengSafetyCoordinator(source_epoch="test-epoch")
        first, _ = coordinator.allocate(
            _telemetry(),
            PccSafetyConstraint("hard", 0.15, "decision-1"),
            pcc_after_confirmed_pv_mw=-1.85,
            confirmed_pv_reduction_mw=0.0,
        )
        second_telemetry = replace(
            _telemetry(wind_p=8.0, storage_p=-2.0),
            snapshot_id="synthetic-snapshot-2",
            elapsed_minutes=1.0,
        )
        second, _ = coordinator.allocate(
            second_telemetry,
            PccSafetyConstraint("hard", 0.15, "decision-2"),
            pcc_after_confirmed_pv_mw=0.05,
            confirmed_pv_reduction_mw=0.0,
        )

        self.assertAlmostEqual(first.wind_storage_target_mw, 6.0)
        self.assertLessEqual(
            second.wind_storage_target_mw,
            first.wind_storage_target_mw,
        )

    def test_capability_shortfall_is_reported_instead_of_faked(self) -> None:
        coordinator = ShanchengSafetyCoordinator(source_epoch="test-epoch")
        result, decision = coordinator.allocate(
            _telemetry(),
            PccSafetyConstraint("hard", 0.15, "decision-1"),
            pcc_after_confirmed_pv_mw=-9.85,
            confirmed_pv_reduction_mw=0.0,
        )

        self.assertTrue(result.command_accepted)
        self.assertAlmostEqual(result.wind_storage_target_mw, 0.0)
        self.assertAlmostEqual(result.allocated_wind_storage_reduction_mw, 8.0)
        self.assertAlmostEqual(result.unserved_reduction_mw, 2.0)
        self.assertIsNotNone(decision)
        self.assertAlmostEqual(
            sum(item.active_power_mw for item in decision.setpoints.wind)
            + decision.setpoints.storage_active_power_mw,
            0.0,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.control_contracts import PccSafetyConstraint
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.execution_coordinator import ShanchengSafetyCoordinator
from oilfield_energy.resource_control_contracts import ResourceSchedule, ResourceType
from oilfield_energy.model import OptimizationResult
from oilfield_energy.shancheng_control import (
    ChannelActuationFeedback,
    SignalValidity,
    ShanchengTelemetry,
    StorageInterlocks,
    StorageTelemetry,
    SvgTelemetry,
    WindTurbineTelemetry,
)


def _telemetry(
    *,
    wind_p: float = 8.0,
    wind_available: float = 10.0,
    storage_p: float = 0.0,
    energy_mwh: float = 2.5,
    active_valid: bool = True,
) -> ShanchengTelemetry:
    validity = (
        SignalValidity(True, True)
        if active_valid else SignalValidity(False, False)
    )
    return ShanchengTelemetry(
        snapshot_id="synthetic-snapshot",
        observed_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        elapsed_minutes=0.0,
        clock_hour=12.0,
        step_minutes=1.0,
        wind_turbines=(WindTurbineTelemetry(
            name="WT-AGG",
            active_power_mw=wind_p,
            reactive_power_mvar=0.0,
            available_active_power_mw=wind_available,
            rated_active_power_mw=10.0,
            active_power_validity=validity,
        ),),
        storage=StorageTelemetry(
            active_power_mw=storage_p,
            energy_mwh=energy_mwh,
            energy_min_mwh=0.5,
            energy_max_mwh=5.0,
            charge_power_max_mw=2.5,
            discharge_power_max_mw=2.5,
            interlocks=StorageInterlocks(),
            active_power_validity=validity,
        ),
        svg=SvgTelemetry(reactive_power_mvar=0.0),
        active_actuation=ChannelActuationFeedback(execution_known=True),
        reactive_actuation=ChannelActuationFeedback(execution_known=True),
    )


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
        self.assertTrue(all(
            item.active_power_mw is None for item in decision.setpoints.wind
        ))

    def test_existing_hard_target_cannot_be_relaxed_by_another_curtailment(self) -> None:
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

    def test_minute_simulation_uses_controller_for_post_pv_deficit(self) -> None:
        source_case = build_synthetic_case(steps=8)
        source = source_case.microgrids[0]
        pv_bus = next(iter(source.pv_available_mw))
        microgrid = replace(
            source,
            # Actual minute injections now determine PCC. A negative planned PCC
            # alone no longer fabricates a load drop; set the gross loads explicitly.
            load_p_mw={bus: values / np.sum(np.vstack(tuple(source.load_p_mw.values())), axis=0) * 3.5
                       for bus, values in source.load_p_mw.items()},
            load_q_mvar={bus: np.zeros(8) for bus in source.load_q_mvar},
            wind_available_mw={bus: np.full(8, 5.0) for bus in source.wind_available_mw},
            pv_available_mw={pv_bus: np.full(8, 0.5)},
            pv_capacity_mw={pv_bus: 0.5},
            pv_capacity_mva={pv_bus: 0.55},
        )
        case = replace(source_case, microgrids=[microgrid])
        zeros = np.zeros(8)
        wind_bus = next(iter(microgrid.wind_available_mw))
        resource_schedules = (
            ResourceSchedule(
                resource_id=f"{microgrid.name}:wind:{wind_bus}",
                bus_id=wind_bus,
                resource_type=ResourceType.WIND,
                active_power_mw=np.full(8, 5.0),
                reactive_power_mvar=zeros,
            ),
            ResourceSchedule(
                resource_id=f"{microgrid.name}:pv:{pv_bus}",
                bus_id=pv_bus,
                resource_type=ResourceType.PV,
                active_power_mw=np.full(8, 0.5),
                reactive_power_mvar=zeros,
            ),
            ResourceSchedule(
                resource_id=f"{microgrid.name}:storage:{microgrid.storage.bus}",
                bus_id=microgrid.storage.bus,
                resource_type=ResourceType.STORAGE,
                active_power_mw=zeros,
                reactive_power_mvar=zeros,
            ),
            ResourceSchedule(
                resource_id=f"{microgrid.name}:svg:{microgrid.svg_bus}",
                bus_id=microgrid.svg_bus,
                resource_type=ResourceType.SVG,
                active_power_mw=zeros,
                reactive_power_mvar=zeros,
            ),
        )
        regional = OptimizationResult(
            success=True,
            status=0,
            message="synthetic safety integration",
            objective_cny=0.0,
            solver_objective_without_constants_cny=0.0,
            mip_gap=0.0,
            microgrids={microgrid.name: {
                "p_grid_mw": np.full(8, -2.0),
                "q_grid_mvar": zeros.copy(),
                "wind_used_mw": np.full(8, 5.0),
                "wind_available_mw": np.full(8, 5.0),
                "pv_used_mw": np.full(8, 0.5),
                "storage_discharge_mw": zeros.copy(),
                "storage_charge_mw": zeros.copy(),
                "wind_q_mvar": zeros.copy(),
                "pv_q_mvar": zeros.copy(),
                "storage_q_mvar": zeros.copy(),
                "svg_q_mvar": zeros.copy(),
                "resource_schedules": resource_schedules,
            }},
            cluster={},
            model_size={},
        )

        result = simulate_device_tracking(case, microgrid, regional)

        self.assertTrue(np.any(result.hard_wind_storage_command_accepted))
        self.assertTrue(np.any(np.isfinite(result.hard_wind_storage_target_mw)))
        first = int(np.flatnonzero(
            result.hard_wind_storage_command_accepted
        )[0])
        self.assertLess(
            result.storage_actual_mw[first],
            0.0,
            "Shancheng decomposition should absorb the first residual in storage",
        )
        self.assertGreaterEqual(
            result.pcc_actual_mw[first],
            case.assumptions.no_reverse_margin_mw - 1e-8,
        )
        self.assertLessEqual(result.hard_safety_unserved_mw[first], 1e-8)
        self.assertTrue(np.all(result.shancheng_reactive_command_accepted))
        self.assertTrue(np.all(result.reactive_execution_known))
        self.assertTrue(np.all(result.reactive_dispatch_unserved_mvar <= 1e-8))
        self.assertEqual(
            set(result.reactive_resource_actual_mvar),
            {item.resource_id for item in resource_schedules},
        )


if __name__ == "__main__":
    unittest.main()

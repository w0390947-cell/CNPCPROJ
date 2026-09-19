from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.shancheng_control import (
    ActiveMarginReporting,
    ActiveRequest,
    ChannelAction,
    ChannelActuationFeedback,
    CommandDisposition,
    CommandEnvelope,
    DeviceStatus,
    LegacyCommandFactory,
    ReactiveRequest,
    SignalValidity,
    ShanchengControlConfig,
    ShanchengControlMode,
    ShanchengController,
    ShanchengTelemetry,
    StorageInterlocks,
    StorageTelemetry,
    SvgTelemetry,
    WindTurbineTelemetry,
    calculate_shancheng_capabilities,
    internal_storage_active_to_southbound,
    internal_svg_reactive_to_southbound,
    internal_wind_active_to_southbound,
    internal_wind_reactive_to_southbound,
    local_shancheng_setpoints,
    northbound_active_target_to_internal,
    northbound_reactive_target_to_internal,
)


_BASE_UTC = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SOURCE_ID = "test-dispatch"
_SOURCE_EPOCH = "test-epoch-1"


def _controller(
    config: ShanchengControlConfig | None = None,
) -> ShanchengController:
    controller = ShanchengController(config)
    controller.authorize_source_epoch(_SOURCE_ID, _SOURCE_EPOCH)
    return controller


def _command(
    message_id: str,
    *,
    elapsed: float = 0.0,
    p: float | None = None,
    q: float | None = None,
    p_seq: int = 1,
    q_seq: int = 1,
    enter_p_local: bool = False,
    enter_q_local: bool = False,
    source_id: str = _SOURCE_ID,
    source_epoch: str = _SOURCE_EPOCH,
    valid_for_minutes: float = 60.0,
) -> CommandEnvelope:
    issued = _BASE_UTC + timedelta(minutes=elapsed)
    active = None
    reactive = None
    if p is not None or enter_p_local:
        active = ActiveRequest(
            p_seq,
            ChannelAction.ENTER_LOCAL if enter_p_local else ChannelAction.SET_TARGET,
            p,
        )
    if q is not None or enter_q_local:
        reactive = ReactiveRequest(
            q_seq,
            ChannelAction.ENTER_LOCAL if enter_q_local else ChannelAction.SET_TARGET,
            q,
        )
    return CommandEnvelope(
        source_id=source_id,
        source_epoch=source_epoch,
        message_id=message_id,
        issued_at_utc=issued,
        valid_until_utc=issued + timedelta(minutes=valid_for_minutes),
        active_request=active,
        reactive_request=reactive,
    )


def _telemetry(
    *,
    elapsed: float = 0.0,
    hour: float = 0.0,
    step: float = 1.0,
    wind_p: tuple[float, float] = (4.0, 4.0),
    wind_available: tuple[float, float] = (5.0, 5.0),
    wind_q: tuple[float, float] = (0.0, 0.0),
    wind_status: tuple[DeviceStatus, DeviceStatus] | None = None,
    storage_p: float = 0.0,
    energy: float = 2.5,
    storage_status: DeviceStatus | None = None,
    interlocks: StorageInterlocks | None = None,
    svg_q: float = 1.5,
    svg_status: DeviceStatus | None = None,
    active_feedback: ChannelActuationFeedback | None = None,
    reactive_feedback: ChannelActuationFeedback | None = None,
) -> ShanchengTelemetry:
    statuses = wind_status or (DeviceStatus(), DeviceStatus())
    return ShanchengTelemetry(
        snapshot_id=f"snapshot-{elapsed}",
        observed_at_utc=_BASE_UTC + timedelta(minutes=elapsed),
        elapsed_minutes=elapsed,
        clock_hour=hour,
        step_minutes=step,
        wind_turbines=tuple(
            WindTurbineTelemetry(
                name=f"WT{index + 1}",
                active_power_mw=wind_p[index],
                reactive_power_mvar=wind_q[index],
                available_active_power_mw=wind_available[index],
                status=statuses[index],
            )
            for index in range(2)
        ),
        storage=StorageTelemetry(
            active_power_mw=storage_p,
            energy_mwh=energy,
            energy_min_mwh=0.5,
            energy_max_mwh=5.0,
            charge_power_max_mw=2.5,
            discharge_power_max_mw=2.5,
            status=storage_status or DeviceStatus(),
            interlocks=interlocks or StorageInterlocks(),
        ),
        svg=SvgTelemetry(
            reactive_power_mvar=svg_q,
            status=svg_status or DeviceStatus(),
        ),
        active_actuation=active_feedback or ChannelActuationFeedback(),
        reactive_actuation=reactive_feedback or ChannelActuationFeedback(),
    )


class ShanchengLocalControlTests(unittest.TestCase):
    def test_local_time_windows_are_left_closed_and_right_open(self) -> None:
        expected = {
            11.999: 0.0,
            12.0: -2.0,
            14.999: -2.0,
            15.0: 0.0,
            19.999: 0.0,
            23.0: 0.0,
        }
        for hour, storage_p in expected.items():
            with self.subTest(hour=hour):
                result = local_shancheng_setpoints(_telemetry(hour=hour))
                self.assertAlmostEqual(result.storage_active_power_mw, storage_p)

    def test_local_discharge_recomputes_average_to_end_of_window(self) -> None:
        # 可输出电量=(2.5-0.5)*0.95=1.9 MWh；20点距23点还有3小时。
        at_twenty = local_shancheng_setpoints(_telemetry(hour=20.0))
        self.assertAlmostEqual(at_twenty.storage_active_power_mw, 1.9 / 3.0)
        at_twenty_two = local_shancheng_setpoints(_telemetry(hour=22.0, energy=1.0))
        self.assertAlmostEqual(at_twenty_two.storage_active_power_mw, 0.475)

    def test_literal_pdf_discharge_formula_remains_configurable(self) -> None:
        result = local_shancheng_setpoints(
            _telemetry(hour=20.0),
            ShanchengControlConfig(local_discharge_accounts_for_efficiency=False),
        )
        self.assertAlmostEqual(result.storage_active_power_mw, 2.0 / 3.0)

    def test_local_soc_limits_stop_charge_and_discharge(self) -> None:
        full = local_shancheng_setpoints(_telemetry(hour=12.0, energy=5.0))
        empty = local_shancheng_setpoints(_telemetry(hour=20.0, energy=0.5))
        self.assertAlmostEqual(full.storage_active_power_mw, 0.0)
        self.assertAlmostEqual(empty.storage_active_power_mw, 0.0)

    def test_local_wind_and_svg_follow_pdf_and_svg_sign_is_converted(self) -> None:
        result = local_shancheng_setpoints(_telemetry(hour=8.0))
        self.assertEqual(tuple(item.active_power_mw for item in result.wind), (5.0, 5.0))
        self.assertEqual(tuple(item.reactive_power_mvar for item in result.wind), (None, None))
        self.assertAlmostEqual(result.svg_capacitive_reactive_power_mvar, 1.5)
        self.assertAlmostEqual(result.svg_southbound_reactive_power_mvar, -1.5)

    def test_remote_fault_and_any_of_25_storage_lockouts_disable_commands(self) -> None:
        bms = (True,) + (False,) * 11
        telemetry = _telemetry(
            hour=12.0,
            wind_status=(DeviceStatus(remote_enabled=False), DeviceStatus(faulted=True)),
            interlocks=StorageInterlocks(bms_lockouts=bms),
            svg_status=DeviceStatus(faulted=True),
        )
        result = local_shancheng_setpoints(telemetry)
        self.assertEqual(tuple(item.active_power_mw for item in result.wind), (None, None))
        self.assertIsNone(result.storage_active_power_mw)
        self.assertIsNone(result.storage_reactive_power_mvar)
        self.assertIsNone(result.svg_capacitive_reactive_power_mvar)

    def test_storage_lockout_contract_requires_12_plus_12_plus_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 12"):
            StorageInterlocks(bms_lockouts=(False,) * 11)


class ShanchengCapabilityTests(unittest.TestCase):
    def test_pdf_reactive_margin_formulas_and_unsigned_values(self) -> None:
        telemetry = _telemetry(
            wind_p=(3.0, 4.0),
            wind_q=(0.2, -0.1),
            svg_q=1.5,
        )
        result = calculate_shancheng_capabilities(telemetry)
        self.assertAlmostEqual(result.wind_active_down_mw, 7.0)
        self.assertAlmostEqual(result.wind_reactive_up_mvar, 2.0)
        self.assertAlmostEqual(result.wind_reactive_down_mvar, 2.2)
        self.assertAlmostEqual(result.svg_reactive_up_mvar, 0.3)
        self.assertAlmostEqual(result.svg_reactive_down_mvar, 3.3)
        self.assertAlmostEqual(result.reactive_up_mvar, 2.3)
        self.assertAlmostEqual(result.reactive_down_mvar, 5.5)
        self.assertTrue(all(value >= 0.0 for value in (
            result.reported_active_down_mw,
            result.reactive_up_mvar,
            result.reactive_down_mvar,
        )))

    def test_active_report_preserves_both_conflicting_pdf_definitions(self) -> None:
        telemetry = _telemetry(storage_p=1.0)
        wind_only = calculate_shancheng_capabilities(telemetry)
        combined = calculate_shancheng_capabilities(
            telemetry,
            ShanchengControlConfig(
                active_margin_reporting=ActiveMarginReporting.WIND_AND_STORAGE
            ),
        )
        self.assertAlmostEqual(wind_only.reported_active_down_mw, 8.0)
        self.assertAlmostEqual(combined.reported_active_down_mw, 11.5)
        self.assertAlmostEqual(combined.storage_transition_down_mw, 3.5)

    def test_unavailable_devices_contribute_no_adjustable_margin(self) -> None:
        unavailable = DeviceStatus(remote_enabled=False)
        result = calculate_shancheng_capabilities(_telemetry(
            wind_status=(unavailable, unavailable),
            storage_status=unavailable,
            svg_status=unavailable,
        ))
        self.assertEqual(result.dispatch_active_down_mw, 0.0)
        self.assertEqual(result.reactive_up_mvar, 0.0)
        self.assertEqual(result.reactive_down_mvar, 0.0)

    def test_unavailable_storage_output_is_a_fixed_target_component(self) -> None:
        unavailable = DeviceStatus(remote_enabled=False)
        telemetry = _telemetry(storage_p=-2.0, storage_status=unavailable)
        capability = calculate_shancheng_capabilities(telemetry)
        self.assertAlmostEqual(capability.maximum_active_target_mw, 8.0)
        controller = _controller()
        decision = controller.step(
            telemetry,
            _command("fixed-storage", p=6.0),
        )
        self.assertEqual(decision.active_disposition, CommandDisposition.ACCEPTED)
        self.assertIsNone(decision.setpoints.storage_active_power_mw)
        self.assertAlmostEqual(
            sum(item.active_power_mw or 0.0 for item in decision.setpoints.wind), 8.0
        )


class ShanchengDispatchTests(unittest.TestCase):
    def test_active_reduction_uses_storage_before_wind(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(),
            _command("p-storage", p=6.5),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.ACCEPTED)
        self.assertEqual(result.active_mode, ShanchengControlMode.DISPATCH)
        self.assertAlmostEqual(result.setpoints.storage_active_power_mw, -1.5)
        self.assertEqual(tuple(item.active_power_mw for item in result.setpoints.wind), (4.0, 4.0))

    def test_active_reduction_curtails_wind_only_after_storage_saturates(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(),
            _command("p-wind", p=4.0),
        )
        self.assertAlmostEqual(result.setpoints.storage_active_power_mw, -2.5)
        wind_total = sum(item.active_power_mw or 0.0 for item in result.setpoints.wind)
        self.assertAlmostEqual(wind_total, 6.5)
        self.assertAlmostEqual(wind_total + result.setpoints.storage_active_power_mw, 4.0)

    def test_upward_dispatch_restores_wind_and_does_not_discharge_by_default(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(wind_p=(3.0, 3.0), wind_available=(4.0, 4.0)),
            _command("p-up", p=7.5),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.ACCEPTED)
        self.assertAlmostEqual(result.setpoints.storage_active_power_mw, 0.0)
        self.assertAlmostEqual(
            sum(item.active_power_mw or 0.0 for item in result.setpoints.wind), 7.5
        )

    def test_storage_discharge_for_upward_dispatch_is_explicit_opt_in(self) -> None:
        telemetry = _telemetry(wind_p=(3.0, 3.0), wind_available=(3.0, 3.0))
        rejected = _controller().step(
            telemetry,
            _command("p-up-default", p=7.0),
        )
        self.assertEqual(rejected.active_disposition, CommandDisposition.REJECTED)

        enabled = _controller(ShanchengControlConfig(
            allow_storage_discharge_for_upward_dispatch=True
        )).step(
            telemetry,
            _command("p-up-storage", p=7.0),
        )
        self.assertEqual(enabled.active_disposition, CommandDisposition.ACCEPTED)
        self.assertAlmostEqual(enabled.setpoints.storage_active_power_mw, 1.0)

    def test_active_out_of_range_is_rejected_without_partial_adjustment(self) -> None:
        controller = _controller()
        telemetry = _telemetry(hour=12.0)
        result = controller.step(
            telemetry,
            _command("p-invalid", p=20.0),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.REJECTED)
        self.assertEqual(result.active_mode, ShanchengControlMode.LOCAL)
        self.assertAlmostEqual(result.setpoints.storage_active_power_mw, -2.0)
        self.assertEqual(tuple(item.active_power_mw for item in result.setpoints.wind), (5.0, 5.0))
        self.assertEqual(result.alarms[0].code, "P_TARGET_OUT_OF_RANGE")

    def test_invalid_new_target_keeps_previously_accepted_dispatch(self) -> None:
        controller = _controller()
        controller.step(
            _telemetry(),
            _command("accepted", p=6.0),
        )
        result = controller.step(
            _telemetry(elapsed=1.0),
            _command("rejected", elapsed=1.0, p=20.0, p_seq=2),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.REJECTED)
        self.assertEqual(result.active_mode, ShanchengControlMode.DISPATCH)
        self.assertAlmostEqual(result.active_target_mw, 6.0)
        self.assertAlmostEqual(result.setpoints.storage_active_power_mw, -2.0)

    def test_reactive_increase_uses_svg_before_wind(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(svg_q=1.0),
            _command("q-up", q=2.2),
        )
        self.assertEqual(result.reactive_disposition, CommandDisposition.ACCEPTED)
        self.assertAlmostEqual(result.setpoints.svg_capacitive_reactive_power_mvar, 1.8)
        wind_q = sum(item.reactive_power_mvar or 0.0 for item in result.setpoints.wind)
        self.assertAlmostEqual(wind_q, 0.4)

    def test_reactive_decrease_uses_wind_before_svg(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(wind_q=(0.5, 0.5), svg_q=1.5),
            _command("q-down", q=1.0),
        )
        self.assertAlmostEqual(result.setpoints.svg_capacitive_reactive_power_mvar, 1.5)
        wind_q = sum(item.reactive_power_mvar or 0.0 for item in result.setpoints.wind)
        self.assertAlmostEqual(wind_q, -0.5)

    def test_reactive_decrease_uses_svg_after_wind_saturates(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(wind_q=(0.0, 0.0), svg_q=1.5),
            _command("q-deep-down", q=-2.0),
        )
        self.assertEqual(result.reactive_disposition, CommandDisposition.ACCEPTED)
        wind_q = sum(item.reactive_power_mvar or 0.0 for item in result.setpoints.wind)
        self.assertAlmostEqual(wind_q, -2.4)
        self.assertAlmostEqual(result.setpoints.svg_capacitive_reactive_power_mvar, 0.4)

    def test_non_atomic_opt_out_can_accept_valid_q_and_reject_invalid_p(self) -> None:
        controller = _controller(ShanchengControlConfig(same_message_atomic=False))
        result = controller.step(
            _telemetry(),
            _command("mixed", p=20.0, q=2.0),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.REJECTED)
        self.assertEqual(result.reactive_disposition, CommandDisposition.ACCEPTED)
        self.assertEqual(result.active_mode, ShanchengControlMode.LOCAL)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.DISPATCH)

    def test_same_message_is_atomic_by_default(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(),
            _command("atomic", p=20.0, q=2.0),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.REJECTED)
        self.assertEqual(result.reactive_disposition, CommandDisposition.REJECTED)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.LOCAL)
        self.assertEqual({alarm.code for alarm in result.alarms}, {"JOINT_PQ_INFEASIBLE"})

    def test_explicit_active_enter_local_does_not_exit_reactive_dispatch(self) -> None:
        controller = _controller()
        controller.step(
            _telemetry(),
            _command("enter", p=6.0, q=2.0),
        )
        result = controller.step(
            _telemetry(elapsed=1.0),
            _command("exit", elapsed=1.0, enter_p_local=True, p_seq=2),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.EXITED_TO_LOCAL)
        self.assertEqual(result.active_mode, ShanchengControlMode.LOCAL)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.DISPATCH)

    def test_p_and_q_time_out_independently(self) -> None:
        controller = _controller(ShanchengControlConfig(
            active_timeout_minutes=5.0,
            reactive_timeout_minutes=10.0,
        ))
        controller.step(
            _telemetry(),
            _command("enter", p=6.0, q=2.0),
        )
        result = controller.step(_telemetry(elapsed=5.0))
        self.assertEqual(result.active_disposition, CommandDisposition.TIMED_OUT)
        self.assertEqual(result.active_mode, ShanchengControlMode.LOCAL)
        self.assertEqual(result.reactive_disposition, CommandDisposition.TRACKING)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.DISPATCH)

    def test_lost_capability_suspends_writes_instead_of_releasing_to_local(self) -> None:
        controller = _controller()
        controller.step(
            _telemetry(),
            _command("enter", p=6.0),
        )
        unavailable = DeviceStatus(remote_enabled=False)
        result = controller.step(_telemetry(
            elapsed=1.0,
            wind_status=(unavailable, unavailable),
            storage_status=unavailable,
        ))
        self.assertEqual(result.active_mode, ShanchengControlMode.DEGRADED_HOLD)
        self.assertEqual(result.active_suspended_target_mw, 6.0)
        self.assertTrue(all(item.active_power_mw is None for item in result.setpoints.wind))
        self.assertIsNone(result.setpoints.storage_active_power_mw)
        self.assertIn(
            "P_TARGET_BECAME_INFEASIBLE", {alarm.code for alarm in result.alarms}
        )

    def test_elapsed_time_must_be_monotonic(self) -> None:
        controller = _controller()
        controller.step(_telemetry(elapsed=2.0))
        with self.assertRaisesRegex(ValueError, "monotonic"):
            controller.step(_telemetry(elapsed=1.0))


class ShanchengSafetyContractTests(unittest.TestCase):
    def test_combined_command_rejects_the_documented_p_q_counterexample(self) -> None:
        controller = _controller()
        unavailable = DeviceStatus(remote_enabled=False)
        result = controller.step(
            _telemetry(
                wind_p=(5.0, 5.0),
                wind_available=(5.0, 5.0),
                svg_q=0.0,
                svg_status=unavailable,
            ),
            _command("joint-envelope", p=2.0, q=3.0),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.REJECTED)
        self.assertEqual(result.reactive_disposition, CommandDisposition.REJECTED)
        self.assertEqual(result.active_mode, ShanchengControlMode.LOCAL)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.LOCAL)
        self.assertIn("JOINT_PQ_INFEASIBLE", {alarm.code for alarm in result.alarms})

    def test_new_p_cannot_preempt_retained_q_or_refresh_its_timeout(self) -> None:
        controller = _controller(ShanchengControlConfig(reactive_timeout_minutes=5.0))
        unavailable = DeviceStatus(remote_enabled=False)
        base = _telemetry(
            wind_p=(5.0, 5.0),
            wind_available=(5.0, 5.0),
            svg_q=0.0,
            svg_status=unavailable,
        )
        accepted = controller.step(base, _command("q-first", q=2.0))
        self.assertEqual(accepted.reactive_disposition, CommandDisposition.ACCEPTED)
        rejected = controller.step(
            replace(
                base,
                snapshot_id="snapshot-4",
                observed_at_utc=_BASE_UTC + timedelta(minutes=4),
                elapsed_minutes=4.0,
            ),
            _command("p-conflict", elapsed=4.0, p=2.0),
        )
        self.assertEqual(rejected.active_disposition, CommandDisposition.REJECTED)
        self.assertEqual(rejected.reactive_target_mvar, 2.0)
        self.assertIn(
            "P_CONFLICTS_WITH_RETAINED_Q", {alarm.code for alarm in rejected.alarms}
        )
        timed_out = controller.step(replace(
            base,
            snapshot_id="snapshot-5",
            observed_at_utc=_BASE_UTC + timedelta(minutes=5),
            elapsed_minutes=5.0,
        ))
        self.assertEqual(timed_out.reactive_disposition, CommandDisposition.TIMED_OUT)

    def test_new_q_cannot_preempt_retained_p(self) -> None:
        controller = _controller()
        unavailable = DeviceStatus(remote_enabled=False)
        base = _telemetry(
            wind_p=(5.0, 5.0),
            wind_available=(5.0, 5.0),
            svg_q=0.0,
            svg_status=unavailable,
        )
        accepted = controller.step(base, _command("p-first", p=2.0))
        self.assertEqual(accepted.active_disposition, CommandDisposition.ACCEPTED)
        rejected = controller.step(
            replace(
                base,
                snapshot_id="snapshot-1",
                observed_at_utc=_BASE_UTC + timedelta(minutes=1),
                elapsed_minutes=1.0,
            ),
            _command("q-conflict", elapsed=1.0, q=3.0),
        )
        self.assertEqual(rejected.reactive_disposition, CommandDisposition.REJECTED)
        self.assertEqual(rejected.active_target_mw, 2.0)
        self.assertIn(
            "Q_CONFLICTS_WITH_RETAINED_P", {alarm.code for alarm in rejected.alarms}
        )

    def test_ten_megawatts_is_an_ordinary_domain_target(self) -> None:
        controller = _controller()
        result = controller.step(_telemetry(), _command("ten", p=10.0))
        self.assertEqual(result.active_disposition, CommandDisposition.ACCEPTED)
        self.assertEqual(result.active_mode, ShanchengControlMode.DISPATCH_TRACKING)
        self.assertEqual(result.active_target_mw, 10.0)

    def test_duplicate_does_not_refresh_deadline(self) -> None:
        controller = _controller(ShanchengControlConfig(active_timeout_minutes=5.0))
        first = _command("first", p=6.0)
        controller.step(_telemetry(), first)
        duplicate = replace(
            first,
            message_id="duplicate",
            issued_at_utc=_BASE_UTC + timedelta(minutes=4),
            valid_until_utc=_BASE_UTC + timedelta(minutes=60),
        )
        duplicate_result = controller.step(_telemetry(elapsed=4.0), duplicate)
        self.assertEqual(duplicate_result.active_disposition, CommandDisposition.DUPLICATE)
        timed_out = controller.step(_telemetry(elapsed=5.0))
        self.assertEqual(timed_out.active_disposition, CommandDisposition.TIMED_OUT)
        self.assertEqual(timed_out.active_mode, ShanchengControlMode.LOCAL)

    def test_same_sequence_with_different_payload_is_a_collision(self) -> None:
        controller = _controller()
        controller.step(_telemetry(), _command("first", p=6.0))
        collision = controller.step(
            _telemetry(elapsed=1.0),
            _command("collision", elapsed=1.0, p=7.0, p_seq=1),
        )
        self.assertEqual(collision.active_disposition, CommandDisposition.REJECTED)
        self.assertEqual(collision.active_target_mw, 6.0)
        self.assertIn("SEQUENCE_COLLISION", {alarm.code for alarm in collision.alarms})

    def test_rejected_new_sequence_is_consumed_as_seen(self) -> None:
        controller = _controller()
        rejected = controller.step(_telemetry(), _command("bad", p=20.0))
        self.assertEqual(rejected.active_disposition, CommandDisposition.REJECTED)
        collision = controller.step(
            _telemetry(elapsed=1.0),
            _command("reuse", elapsed=1.0, p=6.0, p_seq=1),
        )
        self.assertIn("SEQUENCE_COLLISION", {alarm.code for alarm in collision.alarms})

    def test_atomic_replay_consumes_the_other_fresh_sequence(self) -> None:
        controller = _controller()
        controller.step(
            _telemetry(),
            _command("initial", p=6.0, q=2.0, p_seq=2, q_seq=1),
        )
        mixed = controller.step(
            _telemetry(elapsed=1.0),
            _command("mixed-replay", elapsed=1.0, p=7.0, q=1.0, p_seq=1, q_seq=2),
        )
        self.assertEqual(mixed.active_disposition, CommandDisposition.STALE)
        self.assertEqual(mixed.reactive_disposition, CommandDisposition.REJECTED)
        duplicate = controller.step(
            _telemetry(elapsed=2.0),
            _command("q-retry", elapsed=2.0, q=1.0, q_seq=2),
        )
        self.assertEqual(duplicate.reactive_disposition, CommandDisposition.DUPLICATE)

    def test_unauthorized_epoch_does_not_consume_sequence(self) -> None:
        controller = _controller()
        command = _command("new-session", p=6.0, source_epoch="epoch-2")
        rejected = controller.step(_telemetry(), command)
        self.assertIn(
            "SOURCE_EPOCH_NOT_AUTHORIZED", {alarm.code for alarm in rejected.alarms}
        )
        controller.authorize_source_epoch(_SOURCE_ID, "epoch-2")
        accepted = controller.step(_telemetry(), command)
        self.assertEqual(accepted.active_disposition, CommandDisposition.ACCEPTED)

    def test_invalid_measurements_block_outputs_and_capability_bounds(self) -> None:
        controller = _controller()
        telemetry = _telemetry()
        bad_wind = replace(
            telemetry.wind_turbines[0],
            reactive_power_validity=SignalValidity(fresh=False),
        )
        telemetry = replace(
            telemetry,
            wind_turbines=(bad_wind, telemetry.wind_turbines[1]),
        )
        result = controller.step(telemetry)
        self.assertFalse(result.capabilities.active_valid)
        self.assertFalse(result.capabilities.reactive_valid)
        self.assertIsNone(result.capabilities.minimum_active_target_mw)
        self.assertIsNone(result.capabilities.maximum_reactive_target_mvar)
        self.assertEqual(result.active_mode, ShanchengControlMode.OUTPUT_BLOCK)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.OUTPUT_BLOCK)
        self.assertTrue(all(
            item.active_power_mw is None and item.reactive_power_mvar is None
            for item in result.setpoints.wind
        ))

    def test_partial_channel_block_preserves_local_q_field_ownership(self) -> None:
        controller = _controller()
        result = controller.step(_telemetry(
            active_feedback=ChannelActuationFeedback(execution_known=False)
        ))
        self.assertEqual(result.active_mode, ShanchengControlMode.OUTPUT_BLOCK)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.LOCAL)
        self.assertTrue(all(item.active_power_mw is None for item in result.setpoints.wind))
        self.assertIsNone(result.setpoints.storage_active_power_mw)
        self.assertEqual(result.setpoints.storage_reactive_power_mvar, 0.0)
        self.assertEqual(result.setpoints.svg_capacitive_reactive_power_mvar, 1.5)

    def test_recovery_never_automatically_resumes_suspended_target(self) -> None:
        controller = _controller()
        controller.step(_telemetry(), _command("enter", p=6.0))
        unavailable = DeviceStatus(remote_enabled=False)
        lost = controller.step(_telemetry(
            elapsed=1.0,
            wind_status=(unavailable, unavailable),
            storage_status=unavailable,
        ))
        self.assertEqual(lost.active_mode, ShanchengControlMode.DEGRADED_HOLD)
        recovered = controller.step(_telemetry(elapsed=2.0))
        self.assertEqual(recovered.active_mode, ShanchengControlMode.RECOVERY_INHIBIT)
        self.assertIsNone(recovered.active_target_mw)
        self.assertEqual(recovered.active_suspended_target_mw, 6.0)
        self.assertTrue(all(
            item.active_power_mw is None for item in recovered.setpoints.wind
        ))
        resumed = controller.step(
            _telemetry(elapsed=3.0),
            _command("resume", elapsed=3.0, p=6.0, p_seq=2),
        )
        self.assertEqual(resumed.active_mode, ShanchengControlMode.DISPATCH_TRACKING)
        self.assertEqual(resumed.active_disposition, CommandDisposition.ACCEPTED)

    def test_blocked_timeout_never_releases_to_local(self) -> None:
        controller = _controller(ShanchengControlConfig(active_timeout_minutes=5.0))
        controller.step(_telemetry(), _command("enter", p=6.0))
        unknown = ChannelActuationFeedback(execution_known=False)
        blocked = controller.step(_telemetry(elapsed=1.0, active_feedback=unknown))
        self.assertEqual(blocked.active_mode, ShanchengControlMode.OUTPUT_BLOCK)
        expired = controller.step(_telemetry(elapsed=5.0, active_feedback=unknown))
        self.assertEqual(expired.active_mode, ShanchengControlMode.OUTPUT_BLOCK)
        self.assertEqual(
            expired.active_disposition, CommandDisposition.SUSPENDED_EXPIRED
        )
        self.assertTrue(all(item.active_power_mw is None for item in expired.setpoints.wind))

    def test_degraded_timeout_never_releases_to_local_or_saturates(self) -> None:
        controller = _controller(ShanchengControlConfig(active_timeout_minutes=5.0))
        controller.step(_telemetry(), _command("enter", p=6.0))
        unavailable = DeviceStatus(remote_enabled=False)
        controller.step(_telemetry(
            elapsed=1.0,
            wind_status=(unavailable, unavailable),
            storage_status=unavailable,
        ))
        expired = controller.step(_telemetry(
            elapsed=5.0,
            wind_status=(unavailable, unavailable),
            storage_status=unavailable,
        ))
        self.assertEqual(expired.active_mode, ShanchengControlMode.DEGRADED_HOLD)
        self.assertEqual(
            expired.active_disposition, CommandDisposition.SUSPENDED_EXPIRED
        )
        self.assertIsNone(expired.active_suspended_target_mw)
        self.assertTrue(all(item.active_power_mw is None for item in expired.setpoints.wind))

    def test_expired_command_is_seen_but_never_committed(self) -> None:
        controller = _controller()
        expired_command = _command("expired", p=6.0, valid_for_minutes=1.0)
        result = controller.step(_telemetry(elapsed=2.0), expired_command)
        self.assertEqual(result.active_disposition, CommandDisposition.EXPIRED)
        replay = controller.step(
            _telemetry(elapsed=3.0),
            _command("reuse", elapsed=3.0, p=7.0, p_seq=1),
        )
        self.assertIn("SEQUENCE_COLLISION", {alarm.code for alarm in replay.alarms})

    def test_q_execution_unknown_does_not_block_safe_active_tracking(self) -> None:
        controller = _controller()
        result = controller.step(
            _telemetry(
                reactive_feedback=ChannelActuationFeedback(execution_known=False)
            ),
            _command("p-only", p=6.0),
        )
        self.assertEqual(result.active_disposition, CommandDisposition.ACCEPTED)
        self.assertEqual(result.active_mode, ShanchengControlMode.DISPATCH_TRACKING)
        self.assertEqual(result.reactive_mode, ShanchengControlMode.OUTPUT_BLOCK)
        self.assertTrue(all(
            item.reactive_power_mvar is None for item in result.setpoints.wind
        ))
        self.assertIsNone(result.setpoints.svg_capacitive_reactive_power_mvar)

    def test_every_accepted_grid_point_respects_transition_p_q_envelope(self) -> None:
        unavailable = DeviceStatus(remote_enabled=False)
        telemetry = _telemetry(
            wind_p=(5.0, 5.0),
            wind_available=(5.0, 5.0),
            svg_q=0.0,
            svg_status=unavailable,
        )
        for p_target in (2.5, 4.0, 6.0, 8.0, 10.0):
            for q_target in (-3.0, -1.0, 0.0, 1.0, 3.0):
                with self.subTest(p=p_target, q=q_target):
                    result = _controller().step(
                        telemetry,
                        _command("grid", p=p_target, q=q_target),
                    )
                    if result.active_disposition is not CommandDisposition.ACCEPTED:
                        continue
                    for measured, command in zip(
                        telemetry.wind_turbines, result.setpoints.wind, strict=True
                    ):
                        p_effective = min(
                            measured.active_power_mw, command.active_power_mw
                        )
                        q_effective = (
                            measured.reactive_power_mvar
                            if command.reactive_power_mvar is None
                            else command.reactive_power_mvar
                        )
                        self.assertLessEqual(
                            abs(q_effective), 0.3 * p_effective + 1e-9
                        )

    def test_legacy_factory_generates_complete_ordered_metadata(self) -> None:
        factory = LegacyCommandFactory(source_epoch="legacy-epoch")
        first = factory.make(now_utc=_BASE_UTC, active_target_mw=6.0)
        second = factory.make(
            now_utc=_BASE_UTC,
            active_target_mw=7.0,
            reactive_target_mvar=2.0,
        )
        self.assertEqual(first.active_request.sequence, 1)
        self.assertEqual(second.active_request.sequence, 2)
        self.assertEqual(second.reactive_request.sequence, 1)
        self.assertEqual(first.source_epoch, "legacy-epoch")


class ShanchengValidationTests(unittest.TestCase):
    def test_northbound_and_southbound_sign_conversions_are_explicit(self) -> None:
        self.assertEqual(northbound_active_target_to_internal(6.0), 6.0)
        self.assertEqual(northbound_reactive_target_to_internal(1.2), 1.2)
        self.assertEqual(internal_wind_active_to_southbound(3.0), 3.0)
        self.assertEqual(internal_wind_reactive_to_southbound(-0.4), -0.4)
        self.assertEqual(internal_storage_active_to_southbound(-2.0), -2.0)
        self.assertEqual(internal_svg_reactive_to_southbound(1.5), -1.5)

    def test_invalid_configuration_and_telemetry_fail_fast(self) -> None:
        with self.assertRaises(ValueError):
            ShanchengControlConfig(local_svg_capacitive_mvar=2.0)
        with self.assertRaises(ValueError):
            replace(_telemetry(), clock_hour=24.1)
        with self.assertRaises(ValueError):
            CommandEnvelope(
                source_id=_SOURCE_ID,
                source_epoch=_SOURCE_EPOCH,
                message_id="empty",
                issued_at_utc=_BASE_UTC,
                valid_until_utc=_BASE_UTC + timedelta(minutes=1),
            )


if __name__ == "__main__":
    unittest.main()

"""Controller lifecycle and physical boundary regressions for audit item06."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from oilfield_energy.shancheng_control import (
    ChannelActuationFeedback,
    CommandDisposition,
    LegacyCommandFactory,
    ShanchengControlConfig,
    ShanchengController,
    ShanchengControlMode,
    ShanchengTelemetry,
    StorageTelemetry,
    SvgTelemetry,
    WindTurbineTelemetry,
    calculate_shancheng_capabilities,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(*, time=0, hour=0, step=1, power=4, available=5, q=0, energy=5):
    return ShanchengTelemetry(
        snapshot_id=f"limits:{time}:{hour}",
        observed_at_utc=BASE + timedelta(minutes=time),
        elapsed_minutes=time,
        clock_hour=hour,
        step_minutes=step,
        wind_turbines=tuple(WindTurbineTelemetry(str(i), power, q, available) for i in range(2)),
        storage=StorageTelemetry(0, energy, 0.5, 5, 2.5, 2.5),
        svg=SvgTelemetry(1.5),
    )


def setup_controller(config=None):
    controller = ShanchengController(config)
    factory = LegacyCommandFactory(source_id="limits", source_epoch="fixed")
    factory.authorize(controller)
    return controller, factory


def assert_no_writes(decision):
    assert all(
        w.active_power_mw is None and w.reactive_power_mvar is None for w in decision.setpoints.wind
    )
    assert decision.setpoints.storage_active_power_mw is None
    assert decision.setpoints.storage_reactive_power_mvar is None
    assert decision.setpoints.svg_capacitive_reactive_power_mvar is None


def test_old_target_degrades_on_availability_loss_and_needs_fresh_command_after_recovery():
    controller, factory = setup_controller()
    first = snapshot()
    accepted = controller.step(
        first, factory.make(now_utc=BASE, message_id="p6", active_target_mw=6)
    )
    assert accepted.active_disposition is CommandDisposition.ACCEPTED
    dropped = snapshot(time=1, power=3, available=1)
    degraded = controller.step(dropped)
    assert degraded.active_mode is ShanchengControlMode.DEGRADED_HOLD
    assert degraded.capabilities.maximum_active_target_mw == 2
    assert all(w.active_power_mw is None for w in degraded.setpoints.wind)
    restored = snapshot(time=2, power=3, available=5)
    inhibited = controller.step(restored)
    assert inhibited.active_mode is ShanchengControlMode.RECOVERY_INHIBIT
    assert inhibited.active_suspended_target_mw == 6
    assert all(w.active_power_mw is None for w in inhibited.setpoints.wind)
    renewed = controller.step(
        restored,
        factory.make(now_utc=restored.observed_at_utc, message_id="new", active_target_mw=6),
    )
    assert renewed.active_disposition is CommandDisposition.ACCEPTED


def test_all_396_targets_match_independent_current_availability_interval():
    for measured in range(6):
        for available in range(6):
            for target in range(11):
                controller, factory = setup_controller()
                telemetry = snapshot(power=measured, available=available)
                decision = controller.step(
                    telemetry,
                    factory.make(now_utc=BASE, message_id="grid", active_target_mw=target),
                )
                accepted = decision.active_disposition is CommandDisposition.ACCEPTED
                assert accepted == (target <= 2 * available)
                if accepted:
                    assert all(
                        w.active_power_mw <= available + 1e-9 for w in decision.setpoints.wind
                    )
                    assert sum(w.active_power_mw for w in decision.setpoints.wind) == pytest.approx(
                        target, abs=1e-9, rel=0
                    )


def test_local_envelope_failure_latches_until_explicit_reset_even_after_capability_recovers():
    controller, factory = setup_controller()
    blocked = controller.step(snapshot(available=1, q=1.2))
    assert_no_writes(blocked)
    assert blocked.active_mode is ShanchengControlMode.DEGRADED_HOLD
    assert {a.code for a in blocked.alarms} == {
        "P_LOCAL_PLAN_INFEASIBLE",
        "Q_LOCAL_PLAN_INFEASIBLE",
    }
    recovered = snapshot(time=1, q=1.2)
    inhibited = controller.step(recovered)
    assert_no_writes(inhibited)
    assert inhibited.active_mode is ShanchengControlMode.RECOVERY_INHIBIT
    assert inhibited.reactive_mode is ShanchengControlMode.RECOVERY_INHIBIT
    reset = factory.make(
        now_utc=recovered.observed_at_utc,
        message_id="reset",
        enter_active_local=True,
        enter_reactive_local=True,
    )
    resumed = controller.step(recovered, reset)
    assert resumed.active_disposition is CommandDisposition.EXITED_TO_LOCAL
    assert [w.active_power_mw for w in resumed.setpoints.wind] == [5, 5]


def test_explicit_local_transition_rejects_joint_failure_without_preempting_safe_dispatch():
    controller, factory = setup_controller()
    first = snapshot(q=0)
    controller.step(
        first,
        factory.make(
            now_utc=BASE, message_id="dispatch", active_target_mw=6, reactive_target_mvar=1.5
        ),
    )
    # Retained dispatch can reduce Q transactionally; local would leave measured
    # Q unchanged while dropping P, and must not be committed.
    changed = snapshot(time=1, available=1, q=1.2, energy=2.5)
    # P=1, Q=1.5 is reachable: wind 1 MW each, storage -1 MW, wind Q reduced.
    valid = controller.step(
        changed,
        factory.make(
            now_utc=changed.observed_at_utc,
            message_id="safe",
            active_target_mw=1,
            reactive_target_mvar=1.5,
        ),
    )
    assert valid.active_disposition is CommandDisposition.ACCEPTED
    rejected = controller.step(
        changed,
        factory.make(
            now_utc=changed.observed_at_utc,
            message_id="local",
            enter_active_local=True,
            enter_reactive_local=True,
        ),
    )
    assert rejected.active_disposition is CommandDisposition.REJECTED
    assert rejected.active_target_mw == 1
    assert rejected.reactive_target_mvar == 1.5


def test_local_writes_are_blocked_after_healthy_dispatch_timeout_if_envelope_is_unsafe():
    controller, factory = setup_controller(
        ShanchengControlConfig(active_timeout_minutes=1, reactive_timeout_minutes=1)
    )
    telemetry = snapshot(available=1, q=1.2, energy=2.5)
    accepted = controller.step(
        telemetry,
        factory.make(now_utc=BASE, message_id="safe", active_target_mw=1, reactive_target_mvar=1.5),
    )
    assert accepted.active_disposition is CommandDisposition.ACCEPTED
    expired = controller.step(
        replace(
            telemetry,
            snapshot_id="expired",
            elapsed_minutes=1,
            observed_at_utc=BASE + timedelta(minutes=1),
        )
    )
    assert_no_writes(expired)
    assert expired.active_mode is ShanchengControlMode.DEGRADED_HOLD
    assert "P_LOCAL_PLAN_INFEASIBLE" in {a.code for a in expired.alarms}


def test_blocked_active_channel_preserves_safe_local_reactive_ownership():
    telemetry = replace(
        snapshot(), active_actuation=ChannelActuationFeedback(execution_known=False)
    )
    decision = ShanchengController().step(telemetry)
    assert decision.setpoints.storage_active_power_mw is None
    assert decision.setpoints.svg_capacitive_reactive_power_mvar == 1.5


def test_aggregate_up_margin_subtracts_other_turbines_mandatory_drop():
    telemetry = snapshot()
    telemetry = replace(
        telemetry,
        wind_turbines=(
            WindTurbineTelemetry("a", 4, 0, 1),
            WindTurbineTelemetry("b", 1, 0, 5),
        ),
    )
    capability = calculate_shancheng_capabilities(telemetry)
    assert capability.wind_active_up_mw == 4  # Device b's individual headroom.
    assert capability.maximum_active_target_mw == 6
    assert capability.dispatch_active_up_mw == 1  # Net change from measured 5 MW.


@pytest.mark.parametrize("step", [1, 5, 15])
@pytest.mark.parametrize("remaining_fraction", [0.25, 0.5, 1, 2])
@pytest.mark.parametrize("account_efficiency", [True, False])
def test_local_discharge_energy_is_safe_over_declared_interval(
    step, remaining_fraction, account_efficiency
):
    hour = 23 - step * remaining_fraction / 60
    controller = ShanchengController(
        ShanchengControlConfig(local_discharge_accounts_for_efficiency=account_efficiency)
    )
    decision = controller.step(snapshot(hour=hour, step=step, energy=0.51))
    power = decision.setpoints.storage_active_power_mw
    assert power is not None
    end_energy = 0.51 - power * step / 60 / 0.95
    assert 0.5 - 1e-10 <= end_energy <= 0.51
    assert 0 <= power <= 2.5

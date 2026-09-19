from __future__ import annotations

import itertools
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.actuation_arbiter import ActuationArbiter
from oilfield_energy.control_contracts import (
    CapIntentAction,
    GroupTelemetrySnapshot,
    PVStationSnapshot,
    ReverseFlowRiskEstimate,
    SignalValidity,
    StationActuationFeedback,
    StationCapIntent,
)
from oilfield_energy.group_control import GroupControlSupervisor, allocate_pv_curtailment
from oilfield_energy.hard_protection import HardCapReleaseGuard
from oilfield_energy.hierarchy_types import (
    GroupControlAction,
    GroupControlInput,
    GroupControlState,
    PVStationAvailability,
)


def _risk(
    time_minutes: float,
    probability: float | None = 0.2,
    *,
    valid: bool = True,
    as_of: float | None = None,
) -> ReverseFlowRiskEstimate:
    validity = (
        SignalValidity.valid_signal()
        if valid
        else SignalValidity.invalid_signal()
    )
    forecast_as_of = time_minutes if as_of is None else as_of
    return ReverseFlowRiskEstimate(
        probability=probability,
        forecast_as_of_minutes=forecast_as_of,
        horizon_minutes=15,
        valid_until_minutes=max(forecast_as_of, time_minutes + 15.0),
        validity=validity,
    )


def _station(
    station_id: str = "PV-1",
    *,
    validity: SignalValidity | None = None,
    execution_known: bool = True,
) -> PVStationSnapshot:
    signal_validity = validity or SignalValidity.valid_signal()
    return PVStationSnapshot(
        station_id=station_id,
        bus=f"{station_id}-BUS",
        measured_power_mw=2.0,
        available_power_mw=2.0,
        minimum_power_mw=0.0,
        ramp_down_mw_per_minute=None,
        ramp_up_mw_per_minute=None,
        in_service=True,
        healthy=True,
        communication_ok=True,
        controllable=True,
        measured_power_validity=signal_validity,
        available_power_validity=signal_validity,
        status_validity=signal_validity,
        actuation_feedback=StationActuationFeedback(
            None, None, None, execution_known=execution_known
        ),
    )


def _snapshot(
    time_minutes: float,
    pcc_power_mw: float,
    *,
    risk: ReverseFlowRiskEstimate | None = None,
    measured_curtailment_mw: float | None = None,
    hard_active: bool = False,
) -> GroupTelemetrySnapshot:
    stations = (_station(),)
    return GroupTelemetrySnapshot.synthetic(
        snapshot_id=f"snapshot-{time_minutes}",
        time_minutes=time_minutes,
        pcc_power_mw=pcc_power_mw,
        current_net_load_mw=8.0,
        previous_net_load_mw=8.0,
        maximum_load_mw=10.0,
        pv_capacity_mw=4.0,
        p_grid_max_mw=18.0,
        pv_actual_mw=2.0,
        reverse_flow_risk=risk or _risk(time_minutes),
        stations=stations,
        measured_supervisor_curtailment_mw=measured_curtailment_mw,
        achieved_restoration_mw=None,
        hard_protection_active=hard_active,
        controllable_pv_available=True,
        power_factor_within_limits=True,
        storage_soc_within_limits=True,
    )


class ReverseFlowRiskContractTests(unittest.TestCase):
    def test_missing_probability_uses_explicit_conservative_fallback(self) -> None:
        resolved = _risk(5.0, None, valid=False).resolve(
            decision_time_minutes=5.0,
            expected_horizon_minutes=15,
        )
        self.assertIsNone(resolved.observed_probability)
        self.assertEqual(resolved.effective_probability, 1.0)
        self.assertFalse(resolved.valid)
        self.assertEqual(resolved.fallback_reason, "reverse_flow_probability_missing")

    def test_future_as_of_is_rejected_without_leaking_future_information(self) -> None:
        resolved = _risk(5.0, 0.4, as_of=6.0).resolve(
            decision_time_minutes=5.0,
            expected_horizon_minutes=15,
        )
        self.assertFalse(resolved.valid)
        self.assertEqual(resolved.effective_probability, 1.0)
        self.assertEqual(
            resolved.fallback_reason,
            "reverse_flow_probability_future_leakage",
        )

    def test_invalid_probability_blocks_recovery_but_keeps_observation_truthful(self) -> None:
        supervisor = GroupControlSupervisor()
        curtailed = supervisor.step(replace(
            _snapshot(0.0, 0.0),
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
        ))
        self.assertEqual(curtailed.action, GroupControlAction.CURTAIL)

        invalid_risk = _risk(1.0, None, valid=False)
        held = supervisor.step(_snapshot(
            1.0,
            1.0,
            risk=invalid_risk,
            measured_curtailment_mw=curtailed.requested_curtailment_mw,
        ))
        self.assertEqual(held.state, GroupControlState.RECOVERY_INHIBIT)
        self.assertEqual(held.action, GroupControlAction.HOLD)
        self.assertIsNone(held.thresholds.observed_reverse_flow_probability)
        self.assertEqual(held.thresholds.reverse_flow_probability, 1.0)
        self.assertFalse(held.reverse_flow_probability_valid)


class GroupSnapshotSafetyTests(unittest.TestCase):
    def test_invalid_pcc_snapshot_blocks_new_output_and_clears_debounce(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(_snapshot(0.0, 0.0))
        invalid = replace(
            _snapshot(1.0, 0.0),
            pcc_validity=SignalValidity.invalid_signal(),
        )
        decision = supervisor.step(invalid)
        self.assertEqual(decision.state, GroupControlState.OUTPUT_BLOCK)
        self.assertEqual(decision.action, GroupControlAction.NONE)
        self.assertFalse(decision.control_output_valid)
        self.assertEqual(decision.consecutive_risk_count, 0)
        self.assertEqual(decision.observation_window_hits, 0)

    def test_hard_override_blocks_group_action_and_has_release_guard(self) -> None:
        supervisor = GroupControlSupervisor()
        active = supervisor.step(_snapshot(0.0, 0.0, hard_active=True))
        cleared = supervisor.step(_snapshot(1.0, 1.0, hard_active=False))
        next_risk = supervisor.step(_snapshot(2.0, 0.0, hard_active=False))

        self.assertEqual(active.state, GroupControlState.HARD_OVERRIDE)
        self.assertEqual(active.action, GroupControlAction.HOLD)
        self.assertEqual(cleared.trigger_reason, "hard_override_cleared_guard")
        self.assertEqual(next_risk.state, GroupControlState.RISK_OBSERVING)
        self.assertEqual(next_risk.consecutive_risk_count, 1)

    def test_every_five_cycle_pattern_obeys_parallel_three_of_five_rule(self) -> None:
        for pattern in itertools.product((False, True), repeat=5):
            with self.subTest(pattern=pattern):
                supervisor = GroupControlSupervisor()
                actions = []
                for index, violation in enumerate(pattern):
                    decision = supervisor.step(GroupControlInput(
                        time_minutes=float(index),
                        pcc_power_mw=0.0 if violation else 1.0,
                        current_net_load_mw=8.0,
                        previous_net_load_mw=8.0,
                        maximum_load_mw=10.0,
                        pv_capacity_mw=4.0,
                        reverse_flow_probability=0.0,
                        p_grid_max_mw=18.0,
                        pv_actual_mw=2.0,
                    ))
                    actions.append(decision.action is GroupControlAction.CURTAIL)
                has_three_consecutive = any(
                    all(pattern[start:start + 3]) for start in range(3)
                )
                # 频次判据仍以“当前周期正在越限”为动作前提；若第5周期
                # 已恢复安全，不应仅凭历史命中下发一个零量/过期限发动作。
                expected = has_three_consecutive or (
                    pattern[-1] and sum(pattern) >= 3
                )
                self.assertEqual(any(actions), expected)


class ActuationArbiterTests(unittest.TestCase):
    def test_absolute_caps_compose_without_double_subtraction(self) -> None:
        arbiter = ActuationArbiter()
        available = {"PV-1": 10.0}
        result = arbiter.apply(available, (
            StationCapIntent(
                "group", "PV-1", CapIntentAction.SET_CAP, 8.0
            ),
            StationCapIntent(
                "hard", "PV-1", CapIntentAction.SET_CAP, 6.0
            ),
        ))
        self.assertEqual(result[0].effective_cap_mw, 6.0)
        self.assertEqual(dict(result[0].owner_caps), {"group": 8.0, "hard": 6.0})

        after_no_change = arbiter.apply(available, (
            StationCapIntent("group", "PV-1", CapIntentAction.NO_CHANGE),
            StationCapIntent("hard", "PV-1", CapIntentAction.RELEASE_CAP),
        ))
        self.assertEqual(after_no_change[0].effective_cap_mw, 8.0)

        released = arbiter.apply(available, (
            StationCapIntent("group", "PV-1", CapIntentAction.RELEASE_CAP),
        ))
        self.assertEqual(released[0].effective_cap_mw, 10.0)

    def test_counterfactual_resolution_does_not_release_latched_owner(self) -> None:
        arbiter = ActuationArbiter()
        available = {"PV-1": 10.0}
        arbiter.apply(available, (
            StationCapIntent("group", "PV-1", CapIntentAction.SET_CAP, 8.0),
            StationCapIntent("hard", "PV-1", CapIntentAction.SET_CAP, 6.0),
        ))

        preview = arbiter.resolve(available, excluded_owner_ids=("hard",))
        self.assertEqual(preview[0].effective_cap_mw, 8.0)
        self.assertEqual(arbiter.owner_cap("hard", "PV-1"), 6.0)
        actual = arbiter.resolve(available)
        self.assertEqual(actual[0].effective_cap_mw, 6.0)

    def test_stale_station_is_excluded_from_numeric_curtailment_capability(self) -> None:
        stale = _station("STALE", validity=SignalValidity.invalid_signal())
        valid = replace(
            _station("VALID"),
            measured_power_mw=1.0,
            available_power_mw=1.0,
        )
        allocation = allocate_pv_curtailment(1.5, (stale, valid))
        commands = {command.station_name: command for command in allocation.commands}
        self.assertEqual(
            commands["STALE"].availability,
            PVStationAvailability.TELEMETRY_INVALID,
        )
        self.assertFalse(commands["STALE"].capability_valid)
        self.assertEqual(commands["STALE"].allocated_curtailment_mw, 0.0)
        self.assertEqual(commands["VALID"].allocated_curtailment_mw, 1.0)
        self.assertEqual(allocation.unserved_curtailment_mw, 0.5)
        self.assertFalse(allocation.capability_complete)
        self.assertEqual(allocation.invalid_capability_stations, ("STALE",))

    def test_unknown_execution_is_not_reported_as_zero_known_capability(self) -> None:
        unknown = _station("UNKNOWN", execution_known=False)
        allocation = allocate_pv_curtailment(0.5, (unknown,))

        command = allocation.commands[0]
        self.assertEqual(
            command.availability,
            PVStationAvailability.EXECUTION_UNKNOWN,
        )
        self.assertFalse(command.capability_valid)
        self.assertFalse(allocation.capability_complete)
        self.assertEqual(allocation.invalid_capability_stations, ("UNKNOWN",))


class HardCapReleaseGuardTests(unittest.TestCase):
    def test_release_requires_consecutive_counterfactual_safe_cycles(self) -> None:
        guard = HardCapReleaseGuard(hysteresis_mw=0.05, confirmation_cycles=2)
        self.assertFalse(guard.observe(0.19, protection_margin_mw=0.15))
        self.assertEqual(guard.confirmation_count, 0)
        self.assertFalse(guard.observe(0.20, protection_margin_mw=0.15))
        self.assertTrue(guard.observe(0.21, protection_margin_mw=0.15))

    def test_any_unsafe_counterfactual_resets_release_evidence(self) -> None:
        guard = HardCapReleaseGuard(hysteresis_mw=0.05, confirmation_cycles=2)
        self.assertFalse(guard.observe(0.21, protection_margin_mw=0.15))
        self.assertFalse(guard.observe(0.19, protection_margin_mw=0.15))
        self.assertEqual(guard.confirmation_count, 0)


if __name__ == "__main__":
    unittest.main()

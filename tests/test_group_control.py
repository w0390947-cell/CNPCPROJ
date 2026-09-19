from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.group_control import (
    GroupControlSupervisor,
    allocate_pv_curtailment,
    calculate_load_change_rate,
    calculate_pv_penetration,
    calculate_risk_index,
    calculate_thresholds,
)
from oilfield_energy.hierarchy_types import (
    GroupControlAction,
    GroupControlConfig,
    GroupControlDecision,
    GroupControlInput,
    GroupControlState,
    PVStationAvailability,
    PVStationControlInput,
)


def _control_input(**overrides: float) -> GroupControlInput:
    values = {
        "time_minutes": 15.0,
        "pcc_power_mw": 1.0,
        "current_net_load_mw": 8.0,
        "previous_net_load_mw": 8.0,
        "maximum_load_mw": 10.0,
        "pv_capacity_mw": 0.0,
        "reverse_flow_probability": 0.0,
        "p_grid_max_mw": 18.0,
    }
    values.update(overrides)
    return GroupControlInput(**values)


class GroupControlFeatureTests(unittest.TestCase):
    def test_load_change_rate_preserves_sign(self) -> None:
        falling = calculate_load_change_rate(7.2, 8.0, denominator_floor_mw=0.1)
        rising = calculate_load_change_rate(8.8, 8.0, denominator_floor_mw=0.1)
        self.assertAlmostEqual(falling, -0.10)
        self.assertAlmostEqual(rising, 0.10)

    def test_load_change_rate_uses_denominator_floor_near_zero(self) -> None:
        result = calculate_load_change_rate(0.2, 0.0, denominator_floor_mw=1.0)
        self.assertAlmostEqual(result, 0.2)

    def test_pv_penetration_is_capacity_over_maximum_load(self) -> None:
        self.assertAlmostEqual(calculate_pv_penetration(4.2, 9.8), 4.2 / 9.8)

    def test_risk_index_uses_normalized_weighted_features(self) -> None:
        result = calculate_risk_index(
            pv_penetration=0.5,
            load_change_rate=-0.05,
            reverse_flow_probability=0.25,
        )
        self.assertAlmostEqual(result, 0.425)

    def test_risk_features_are_clipped_to_their_reference_levels(self) -> None:
        result = calculate_risk_index(
            pv_penetration=4.0,
            load_change_rate=2.0,
            reverse_flow_probability=1.0,
        )
        self.assertAlmostEqual(result, 1.0)

    def test_invalid_feature_inputs_are_rejected(self) -> None:
        with self.subTest("negative PV capacity"):
            with self.assertRaisesRegex(ValueError, "pv_capacity_mw"):
                calculate_pv_penetration(-1.0, 10.0)
        with self.subTest("zero maximum load"):
            with self.assertRaisesRegex(ValueError, "maximum_load_mw"):
                calculate_pv_penetration(1.0, 0.0)
        with self.subTest("invalid probability"):
            with self.assertRaisesRegex(ValueError, "reverse_flow_probability"):
                calculate_risk_index(0.5, 0.05, 1.01)
        with self.subTest("non-finite load"):
            with self.assertRaisesRegex(ValueError, "current_net_load_mw"):
                calculate_load_change_rate(math.inf, 1.0, denominator_floor_mw=0.1)
        with self.subTest("non-numeric load"):
            with self.assertRaisesRegex(ValueError, "current_net_load_mw"):
                calculate_load_change_rate(  # type: ignore[arg-type]
                    "1.0", 1.0, denominator_floor_mw=0.1
                )
        with self.subTest("non-positive denominator floor"):
            with self.assertRaisesRegex(ValueError, "denominator_floor_mw"):
                calculate_load_change_rate(1.0, 1.0, denominator_floor_mw=0.0)


class GroupControlThresholdTests(unittest.TestCase):
    def test_default_low_risk_thresholds_match_synthetic_scale(self) -> None:
        snapshot = calculate_thresholds(_control_input())
        self.assertAlmostEqual(snapshot.risk_index, 0.0)
        self.assertAlmostEqual(snapshot.risk_limit_mw, 0.05)
        self.assertAlmostEqual(snapshot.safety_threshold_mw, 0.15)
        self.assertAlmostEqual(snapshot.restore_threshold_mw, 0.25)
        self.assertFalse(snapshot.was_clipped)

    def test_thresholds_are_strictly_ordered(self) -> None:
        for probability in (0.0, 0.25, 0.5, 0.75, 1.0):
            with self.subTest(probability=probability):
                snapshot = calculate_thresholds(_control_input(
                    current_net_load_mw=7.4,
                    previous_net_load_mw=8.0,
                    pv_capacity_mw=4.2,
                    reverse_flow_probability=probability,
                ))
                self.assertGreater(snapshot.risk_limit_mw, 0.0)
                self.assertLess(snapshot.risk_limit_mw, snapshot.safety_threshold_mw)
                self.assertLess(snapshot.safety_threshold_mw, snapshot.restore_threshold_mw)
                self.assertLessEqual(snapshot.restore_threshold_mw, 18.0)

    def test_higher_risk_tightens_curtailment_and_restore_thresholds(self) -> None:
        low = calculate_thresholds(_control_input())
        high = calculate_thresholds(_control_input(
            current_net_load_mw=6.0,
            previous_net_load_mw=8.0,
            pv_capacity_mw=10.0,
            reverse_flow_probability=1.0,
        ))
        self.assertGreater(high.safety_threshold_mw, low.safety_threshold_mw)
        self.assertGreater(high.risk_limit_mw, low.risk_limit_mw)
        self.assertLess(high.risk_buffer_mw, low.risk_buffer_mw)
        self.assertGreater(high.restore_buffer_mw, low.restore_buffer_mw)
        self.assertGreater(high.restore_threshold_mw, low.restore_threshold_mw)

    def test_threshold_calculation_reports_feature_clipping(self) -> None:
        snapshot = calculate_thresholds(_control_input(
            current_net_load_mw=4.0,
            previous_net_load_mw=8.0,
            pv_capacity_mw=20.0,
        ))
        self.assertTrue(snapshot.was_clipped)
        self.assertAlmostEqual(snapshot.normalized_pv_penetration, 1.0)
        self.assertAlmostEqual(snapshot.normalized_load_change_rate, 1.0)

    def test_threshold_calculation_is_deterministic(self) -> None:
        control_input = _control_input(
            current_net_load_mw=7.5,
            pv_capacity_mw=4.2,
            reverse_flow_probability=0.35,
        )
        self.assertEqual(
            calculate_thresholds(control_input),
            calculate_thresholds(control_input),
        )

    def test_pcc_capacity_that_cannot_accommodate_thresholds_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "restore threshold exceeds"):
            calculate_thresholds(_control_input(p_grid_max_mw=0.20))

    def test_invalid_runtime_inputs_are_rejected(self) -> None:
        invalid = [
            ("time_minutes", -1.0),
            ("pcc_power_mw", math.nan),
            ("maximum_load_mw", 0.0),
            ("pv_capacity_mw", -0.1),
            ("reverse_flow_probability", -0.01),
            ("p_grid_max_mw", 0.0),
        ]
        for field, value in invalid:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    calculate_thresholds(_control_input(**{field: value}))

    def test_state_enum_has_stable_serializable_values(self) -> None:
        self.assertEqual(GroupControlState.NORMAL.value, "normal")
        self.assertEqual(GroupControlState.RESTORING.value, "restoring")


class GroupControlConfigurationTests(unittest.TestCase):
    def test_invalid_configurations_are_rejected(self) -> None:
        invalid_configs = [
            {"weight_pv_penetration": 0.5},
            {"weight_pv_penetration": "0.3"},
            {"weight_load_change_rate": -0.1, "weight_pv_penetration": 0.4,
             "weight_reverse_flow_probability": 0.7},
            {"base_safety_margin_ratio": 0.01, "maximum_risk_buffer_ratio": 0.01},
            {"minimum_risk_buffer_ratio": 0.02, "maximum_risk_buffer_ratio": 0.01},
            {"minimum_restore_buffer_ratio": 0.04, "maximum_restore_buffer_ratio": 0.03},
            {"decision_interval_minutes": 4},
            {"decision_interval_minutes": 1.0},
            {"decision_interval_minutes": True},
            {"recovery_step_fraction": 0.0},
            {"response_relative_tolerance": 1.1},
            {"observation_window_size": 2, "consecutive_trigger_count": 3},
            {"observation_window_size": 2, "observation_trigger_count": 3},
        ]
        for parameters in invalid_configs:
            with self.subTest(parameters=parameters):
                with self.assertRaises(ValueError):
                    GroupControlConfig(**parameters)

    def test_reference_configuration_is_valid(self) -> None:
        config = GroupControlConfig()
        self.assertEqual(config.feature_interval_minutes, 15)
        self.assertEqual(config.decision_interval_minutes, 1)
        self.assertEqual(config.consecutive_trigger_count, 3)
        self.assertEqual(config.observation_window_size, 5)
        self.assertEqual(config.observation_trigger_count, 3)
        self.assertEqual(config.recovery_pause_minutes, 3)


class GroupControlSupervisorTests(unittest.TestCase):
    @staticmethod
    def _step_input(
        time_minutes: float,
        pcc_power_mw: float,
        **overrides: float,
    ) -> GroupControlInput:
        values = {
            "time_minutes": time_minutes,
            "pcc_power_mw": pcc_power_mw,
            "current_net_load_mw": 8.0,
            "previous_net_load_mw": 8.0,
            "maximum_load_mw": 10.0,
            "pv_capacity_mw": 4.0,
            "reverse_flow_probability": 0.0,
            "p_grid_max_mw": 18.0,
            "pv_actual_mw": 2.0,
        }
        values.update(overrides)
        return GroupControlInput(**values)

    def test_three_consecutive_violations_trigger_curtailment(self) -> None:
        supervisor = GroupControlSupervisor()
        first = supervisor.step(self._step_input(0.0, 0.0))
        second = supervisor.step(self._step_input(1.0, 0.0))
        third = supervisor.step(self._step_input(2.0, 0.0))

        self.assertEqual(first.state, GroupControlState.RISK_OBSERVING)
        self.assertEqual(first.action, GroupControlAction.NONE)
        self.assertEqual(second.action, GroupControlAction.NONE)
        self.assertEqual(third.action, GroupControlAction.CURTAIL)
        self.assertEqual(third.trigger_reason, "consecutive_risk_limit")
        self.assertFalse(third.immediate)
        self.assertEqual(third.consecutive_risk_count, 3)

    def test_three_violations_in_five_cycles_trigger_curtailment(self) -> None:
        supervisor = GroupControlSupervisor()
        decisions = [
            supervisor.step(self._step_input(float(index), pcc))
            for index, pcc in enumerate((0.0, 1.0, 0.0, 1.0, 0.0))
        ]

        self.assertTrue(all(item.action is GroupControlAction.NONE for item in decisions[:-1]))
        self.assertEqual(decisions[-1].action, GroupControlAction.CURTAIL)
        self.assertEqual(decisions[-1].trigger_reason, "frequency_risk_limit")
        self.assertEqual(decisions[-1].observation_window_hits, 3)

    def test_emergency_load_change_triggers_on_first_violation(self) -> None:
        supervisor = GroupControlSupervisor()
        decision = supervisor.step(self._step_input(
            0.0,
            0.0,
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
        ))

        self.assertEqual(decision.action, GroupControlAction.CURTAIL)
        self.assertEqual(decision.trigger_reason, "emergency_load_change_rate")
        self.assertTrue(decision.immediate)

    def test_active_curtailment_uses_immediate_secondary_control(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(self._step_input(
            0.0,
            0.0,
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
        ))
        decision = supervisor.step(self._step_input(1.0, 0.0))

        self.assertEqual(decision.action, GroupControlAction.CURTAIL)
        self.assertEqual(decision.trigger_reason, "secondary_control")
        self.assertTrue(decision.immediate)

    def test_transient_violations_do_not_trigger_control(self) -> None:
        supervisor = GroupControlSupervisor()
        decisions = [
            supervisor.step(self._step_input(float(index), pcc))
            for index, pcc in enumerate((0.0, 0.0, 1.0, 1.0, 1.0))
        ]
        self.assertTrue(all(item.action is GroupControlAction.NONE for item in decisions))
        self.assertFalse(supervisor.curtailment_active)

    def test_curtailment_request_is_limited_by_available_pv(self) -> None:
        supervisor = GroupControlSupervisor()
        decision = supervisor.step(self._step_input(
            0.0,
            -1.0,
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
            pv_actual_mw=0.25,
        ))

        self.assertGreater(decision.required_curtailment_mw, 0.25)
        self.assertAlmostEqual(decision.requested_curtailment_mw, 0.25)
        self.assertAlmostEqual(
            decision.unserved_curtailment_mw,
            decision.required_curtailment_mw - 0.25,
        )

    def test_recovery_threshold_only_enters_wait_state(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(self._step_input(
            0.0,
            0.0,
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
        ))
        decision = supervisor.step(self._step_input(1.0, 1.0))

        self.assertEqual(decision.state, GroupControlState.RESTORE_WAIT)
        self.assertEqual(decision.action, GroupControlAction.HOLD)
        self.assertEqual(decision.requested_restoration_mw, 0.0)
        self.assertTrue(supervisor.curtailment_active)

    def test_recovery_threshold_equality_remains_in_hold(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(self._step_input(
            0.0,
            0.0,
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
        ))
        normal_features = self._step_input(1.0, 1.0)
        restore_threshold = calculate_thresholds(normal_features).restore_threshold_mw
        decision = supervisor.step(self._step_input(1.0, restore_threshold))

        self.assertEqual(decision.state, GroupControlState.CURTAILED_HOLD)
        self.assertEqual(decision.action, GroupControlAction.HOLD)

    def test_risk_limit_equality_is_not_a_violation(self) -> None:
        supervisor = GroupControlSupervisor()
        initial = self._step_input(0.0, 1.0)
        threshold = calculate_thresholds(initial).risk_limit_mw
        decision = supervisor.step(self._step_input(0.0, threshold))

        self.assertEqual(decision.consecutive_risk_count, 0)
        self.assertEqual(decision.action, GroupControlAction.NONE)

    def test_sampling_gap_clears_trigger_evidence(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(self._step_input(0.0, 0.0))
        supervisor.step(self._step_input(1.0, 0.0))
        after_gap = supervisor.step(self._step_input(3.0, 0.0))

        self.assertEqual(after_gap.consecutive_risk_count, 1)
        self.assertEqual(after_gap.observation_window_hits, 1)
        self.assertEqual(after_gap.action, GroupControlAction.NONE)

    def test_invalid_timestamp_and_pv_measurement_are_rejected(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(self._step_input(1.0, 1.0))
        for time_minutes in (1.0, 0.0, 1.5):
            with self.subTest(time_minutes=time_minutes):
                with self.assertRaises(ValueError):
                    supervisor.step(self._step_input(time_minutes, 1.0))

        fresh = GroupControlSupervisor()
        with self.assertRaisesRegex(ValueError, "pv_actual_mw"):
            fresh.step(self._step_input(0.0, 1.0, pv_actual_mw=-0.1))

    def test_event_log_is_immutable_and_reset_clears_runtime_state(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(self._step_input(0.0, 0.0))
        supervisor.step(self._step_input(1.0, 0.0))
        supervisor.step(self._step_input(2.0, 0.0))

        self.assertIsInstance(supervisor.events, tuple)
        self.assertEqual(supervisor.events[-1].event_type, "curtailment_command")
        self.assertGreater(supervisor.events[-1].requested_power_mw, 0.0)

        supervisor.reset()
        self.assertEqual(supervisor.state, GroupControlState.NORMAL)
        self.assertFalse(supervisor.curtailment_active)
        self.assertEqual(supervisor.events, ())


class GroupControlRecoveryTests(unittest.TestCase):
    @staticmethod
    def _input(
        time_minutes: float,
        pcc_power_mw: float,
        **overrides: object,
    ) -> GroupControlInput:
        values = {
            "time_minutes": time_minutes,
            "pcc_power_mw": pcc_power_mw,
            "current_net_load_mw": 8.0,
            "previous_net_load_mw": 8.0,
            "maximum_load_mw": 10.0,
            "pv_capacity_mw": 4.0,
            "reverse_flow_probability": 0.0,
            "p_grid_max_mw": 18.0,
            "pv_actual_mw": 2.0,
        }
        values.update(overrides)
        return GroupControlInput(**values)

    def _start_recovery(
        self,
        *,
        config: GroupControlConfig | None = None,
    ) -> tuple[GroupControlSupervisor, GroupControlDecision]:
        supervisor = GroupControlSupervisor(config)
        supervisor.step(self._input(
            0.0,
            0.0,
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
        ))
        wait = supervisor.step(self._input(1.0, 1.0))
        self.assertEqual(wait.state, GroupControlState.RESTORE_WAIT)
        restoration = supervisor.step(self._input(2.0, 1.0))
        self.assertEqual(restoration.action, GroupControlAction.RESTORE)
        return supervisor, restoration

    def test_recovery_command_waits_full_three_minutes_before_evaluation(self) -> None:
        config = GroupControlConfig(recovery_step_fraction=1.0)
        supervisor, restoration = self._start_recovery(config=config)

        first_hold = supervisor.step(self._input(3.0, 1.0))
        second_hold = supervisor.step(self._input(4.0, 1.0))
        completed = supervisor.step(self._input(
            5.0,
            1.0,
            achieved_restoration_mw=restoration.requested_restoration_mw,
        ))

        self.assertEqual(first_hold.state, GroupControlState.RESTORING)
        self.assertEqual(first_hold.action, GroupControlAction.HOLD)
        self.assertAlmostEqual(first_hold.recovery_dwell_remaining_minutes, 2.0)
        self.assertAlmostEqual(second_hold.recovery_dwell_remaining_minutes, 1.0)
        self.assertEqual(completed.state, GroupControlState.NORMAL)
        self.assertTrue(completed.recovery_evaluation_passed)
        self.assertAlmostEqual(completed.remaining_curtailment_mw, 0.0)
        self.assertFalse(supervisor.curtailment_active)

    def test_successful_partial_step_returns_to_restore_wait(self) -> None:
        supervisor, restoration = self._start_recovery()
        initial_remaining = restoration.remaining_curtailment_mw
        supervisor.step(self._input(3.0, 1.0))
        supervisor.step(self._input(4.0, 1.0))
        evaluated = supervisor.step(self._input(
            5.0,
            1.0,
            achieved_restoration_mw=restoration.requested_restoration_mw,
        ))

        self.assertEqual(evaluated.state, GroupControlState.RESTORE_WAIT)
        self.assertTrue(evaluated.recovery_evaluation_passed)
        self.assertAlmostEqual(
            evaluated.remaining_curtailment_mw,
            initial_remaining - restoration.requested_restoration_mw,
        )
        next_step = supervisor.step(self._input(6.0, 1.0))
        self.assertEqual(next_step.action, GroupControlAction.RESTORE)
        self.assertAlmostEqual(
            next_step.requested_restoration_mw,
            restoration.requested_restoration_mw,
        )

    def test_measured_curtailment_overrides_internal_command_estimate(self) -> None:
        supervisor = GroupControlSupervisor()
        supervisor.step(self._input(
            0.0,
            0.0,
            current_net_load_mw=6.8,
            previous_net_load_mw=8.0,
        ))
        waiting = supervisor.step(self._input(
            1.0,
            1.0,
            measured_curtailment_mw=0.20,
        ))
        restoration = supervisor.step(self._input(2.0, 1.0))

        self.assertAlmostEqual(waiting.remaining_curtailment_mw, 0.20)
        self.assertAlmostEqual(restoration.requested_restoration_mw, 0.02)

    def test_response_mismatch_aborts_recovery(self) -> None:
        supervisor, restoration = self._start_recovery()
        supervisor.step(self._input(3.0, 1.0))
        supervisor.step(self._input(4.0, 1.0))
        failed = supervisor.step(self._input(
            5.0,
            1.0,
            achieved_restoration_mw=0.5 * restoration.requested_restoration_mw,
        ))

        self.assertEqual(failed.state, GroupControlState.RECOVERY_ABORTED)
        self.assertEqual(failed.action, GroupControlAction.ABORT_RECOVERY)
        self.assertEqual(failed.trigger_reason, "restoration_response_mismatch")
        self.assertFalse(failed.recovery_evaluation_passed)
        self.assertAlmostEqual(
            failed.remaining_curtailment_mw,
            restoration.remaining_curtailment_mw
            - 0.5 * restoration.requested_restoration_mw,
        )

    def test_missing_feedback_aborts_when_dwell_expires(self) -> None:
        supervisor, _ = self._start_recovery()
        supervisor.step(self._input(3.0, 1.0))
        supervisor.step(self._input(4.0, 1.0))
        failed = supervisor.step(self._input(5.0, 1.0))

        self.assertEqual(failed.action, GroupControlAction.ABORT_RECOVERY)
        self.assertEqual(failed.trigger_reason, "missing_restoration_feedback")

    def test_safety_violation_aborts_during_dwell(self) -> None:
        supervisor, _ = self._start_recovery()
        failed = supervisor.step(self._input(
            3.0,
            1.0,
            voltage_within_limits=False,
        ))

        self.assertEqual(failed.state, GroupControlState.RECOVERY_ABORTED)
        self.assertEqual(failed.action, GroupControlAction.ABORT_RECOVERY)
        self.assertEqual(failed.trigger_reason, "voltage_limit_violation")

    def test_pcc_drop_below_safety_holds_without_latching_execution_failure(self) -> None:
        supervisor, _ = self._start_recovery()
        failed = supervisor.step(self._input(3.0, 0.15))

        self.assertEqual(failed.state, GroupControlState.CURTAILED_HOLD)
        self.assertEqual(failed.action, GroupControlAction.HOLD)
        self.assertEqual(failed.trigger_reason, "recovery_safety_margin_lost")
        self.assertFalse(supervisor.recovery_inhibited)

    def test_sampling_gap_aborts_recovery_monitoring(self) -> None:
        supervisor, _ = self._start_recovery()
        failed = supervisor.step(self._input(6.0, 1.0))

        self.assertEqual(failed.action, GroupControlAction.ABORT_RECOVERY)
        self.assertEqual(failed.trigger_reason, "recovery_monitoring_gap")

    def test_new_risk_during_recovery_immediately_curtails_again(self) -> None:
        supervisor, _ = self._start_recovery()
        decision = supervisor.step(self._input(3.0, 0.0))

        self.assertEqual(decision.state, GroupControlState.CURTAILING)
        self.assertEqual(decision.action, GroupControlAction.CURTAIL)
        self.assertEqual(decision.trigger_reason, "secondary_control")
        self.assertTrue(decision.immediate)

    def test_aborted_recovery_requires_explicit_rearm_before_retry(self) -> None:
        supervisor, _ = self._start_recovery()
        aborted = supervisor.step(self._input(
            3.0,
            1.0,
            recovery_command_valid=False,
        ))
        inhibited = supervisor.step(self._input(4.0, 1.0))
        rearmed = supervisor.step(self._input(
            5.0,
            1.0,
            recovery_rearm_requested=True,
        ))
        ready = supervisor.step(self._input(6.0, 1.0))
        retried = supervisor.step(self._input(7.0, 1.0))

        self.assertEqual(aborted.state, GroupControlState.RECOVERY_ABORTED)
        self.assertEqual(inhibited.state, GroupControlState.RECOVERY_INHIBIT)
        self.assertEqual(rearmed.state, GroupControlState.CURTAILED_HOLD)
        self.assertEqual(ready.state, GroupControlState.RESTORE_WAIT)
        self.assertEqual(ready.action, GroupControlAction.HOLD)
        self.assertEqual(retried.action, GroupControlAction.RESTORE)

    def test_invalid_recovery_feedback_is_rejected(self) -> None:
        invalid_overrides = (
            {"measured_curtailment_mw": -0.1},
            {"measured_curtailment_mw": 4.1},
            {"achieved_restoration_mw": -0.1},
            {"recovery_command_acknowledged": 1},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                supervisor = GroupControlSupervisor()
                with self.assertRaises(ValueError):
                    supervisor.step(self._input(0.0, 1.0, **overrides))

    def test_recovery_events_distinguish_command_completion_and_abort(self) -> None:
        config = GroupControlConfig(recovery_step_fraction=1.0)
        supervisor, restoration = self._start_recovery(config=config)
        supervisor.step(self._input(3.0, 1.0))
        supervisor.step(self._input(4.0, 1.0))
        supervisor.step(self._input(
            5.0,
            1.0,
            achieved_restoration_mw=restoration.requested_restoration_mw,
        ))

        restoration_events = [
            event for event in supervisor.events
            if event.event_type == "restoration_command"
        ]
        self.assertEqual(len(restoration_events), 1)
        self.assertAlmostEqual(
            restoration_events[0].requested_power_mw,
            restoration.requested_restoration_mw,
        )
        self.assertEqual(supervisor.events[-1].reason, "recovery_completed")
        self.assertAlmostEqual(
            supervisor.events[-1].requested_power_mw,
            restoration.requested_restoration_mw,
        )
        self.assertAlmostEqual(
            supervisor.events[-1].achieved_power_mw,
            restoration.requested_restoration_mw,
        )


class PVCurtailmentAllocationTests(unittest.TestCase):
    @staticmethod
    def _station(
        name: str,
        actual_power_mw: float,
        **overrides: object,
    ) -> PVStationControlInput:
        values = {
            "station_name": name,
            "bus": f"{name}_BUS",
            "actual_power_mw": actual_power_mw,
        }
        values.update(overrides)
        return PVStationControlInput(**values)

    def test_allocation_is_proportional_to_current_output(self) -> None:
        result = allocate_pv_curtailment(
            0.9,
            (self._station("PV_A", 2.0), self._station("PV_B", 1.0)),
        )

        self.assertAlmostEqual(result.commands[0].allocated_curtailment_mw, 0.6)
        self.assertAlmostEqual(result.commands[1].allocated_curtailment_mw, 0.3)
        self.assertAlmostEqual(result.commands[0].target_power_mw, 1.4)
        self.assertAlmostEqual(result.allocated_curtailment_mw, 0.9)
        self.assertAlmostEqual(result.unserved_curtailment_mw, 0.0)
        self.assertTrue(result.fully_allocated)

    def test_saturated_share_is_redistributed_to_remaining_station(self) -> None:
        result = allocate_pv_curtailment(
            1.0,
            (
                self._station("PV_A", 4.0, ramp_down_mw_per_minute=0.2),
                self._station("PV_B", 2.0),
            ),
        )
        first, second = result.commands

        self.assertAlmostEqual(first.initial_proportional_request_mw, 2.0 / 3.0)
        self.assertAlmostEqual(first.allocated_curtailment_mw, 0.2)
        self.assertTrue(first.saturated)
        self.assertEqual(first.limiting_constraint, "ramp_down_limit")
        self.assertAlmostEqual(second.initial_proportional_request_mw, 1.0 / 3.0)
        self.assertAlmostEqual(second.allocated_curtailment_mw, 0.8)
        self.assertAlmostEqual(result.allocated_curtailment_mw, 1.0)

    def test_unavailable_stations_are_excluded_with_explicit_reasons(self) -> None:
        stations = (
            self._station("OFF", 1.0, is_in_service=False),
            self._station("FAULT", 1.0, is_healthy=False),
            self._station("COMMS", 1.0, communication_available=False),
            self._station("MANUAL", 1.0, is_controllable=False),
            self._station("READY", 2.0),
        )
        result = allocate_pv_curtailment(1.0, stations)

        self.assertEqual(
            [command.availability for command in result.commands],
            [
                PVStationAvailability.OUT_OF_SERVICE,
                PVStationAvailability.FAULTED,
                PVStationAvailability.COMMUNICATION_LOST,
                PVStationAvailability.UNCONTROLLABLE,
                PVStationAvailability.AVAILABLE,
            ],
        )
        self.assertTrue(all(
            command.allocated_curtailment_mw == 0.0
            for command in result.commands[:-1]
        ))
        self.assertAlmostEqual(result.commands[-1].allocated_curtailment_mw, 1.0)

    def test_minimum_power_and_ramp_limits_report_unserved_request(self) -> None:
        result = allocate_pv_curtailment(
            2.0,
            (
                self._station("PV_A", 1.0, minimum_power_mw=0.6),
                self._station("PV_B", 1.0, ramp_down_mw_per_minute=0.25),
            ),
        )

        self.assertAlmostEqual(result.commands[0].available_curtailment_mw, 0.4)
        self.assertAlmostEqual(result.commands[0].target_power_mw, 0.6)
        self.assertEqual(result.commands[0].limiting_constraint, "minimum_power_limit")
        self.assertAlmostEqual(result.commands[1].available_curtailment_mw, 0.25)
        self.assertEqual(result.commands[1].limiting_constraint, "ramp_down_limit")
        self.assertAlmostEqual(result.allocated_curtailment_mw, 0.65)
        self.assertAlmostEqual(result.unserved_curtailment_mw, 1.35)
        self.assertFalse(result.fully_allocated)

    def test_decision_interval_scales_ramp_down_capability(self) -> None:
        station = self._station("PV_A", 2.0, ramp_down_mw_per_minute=0.2)
        result = allocate_pv_curtailment(
            1.0,
            (station,),
            decision_interval_minutes=2.0,
        )

        self.assertAlmostEqual(result.allocated_curtailment_mw, 0.4)
        self.assertAlmostEqual(result.unserved_curtailment_mw, 0.6)

    def test_zero_request_preserves_station_targets_and_order(self) -> None:
        stations = (self._station("PV_B", 1.0), self._station("PV_A", 2.0))
        result = allocate_pv_curtailment(0.0, stations)

        self.assertEqual(tuple(item.station_name for item in result.commands), ("PV_B", "PV_A"))
        self.assertEqual(tuple(item.target_power_mw for item in result.commands), (1.0, 2.0))
        self.assertTrue(result.fully_allocated)

    def test_invalid_station_contracts_and_allocation_inputs_are_rejected(self) -> None:
        invalid_stations = [
            {"name": "", "actual_power_mw": 1.0},
            {"name": "PV", "actual_power_mw": -1.0},
            {"name": "PV", "actual_power_mw": 1.0, "minimum_power_mw": 1.1},
            {
                "name": "PV",
                "actual_power_mw": 1.0,
                "ramp_down_mw_per_minute": -0.1,
            },
            {"name": "PV", "actual_power_mw": 1.0, "is_healthy": 1},
        ]
        for parameters in invalid_stations:
            with self.subTest(parameters=parameters):
                with self.assertRaises(ValueError):
                    self._station(**parameters)

        station = self._station("PV", 1.0)
        with self.assertRaisesRegex(ValueError, "requested_curtailment_mw"):
            allocate_pv_curtailment(-0.1, (station,))
        with self.assertRaisesRegex(ValueError, "decision_interval_minutes"):
            allocate_pv_curtailment(0.1, (station,), decision_interval_minutes=0.0)
        with self.assertRaisesRegex(ValueError, "unique"):
            allocate_pv_curtailment(0.1, (station, station))
        with self.assertRaises(TypeError):
            allocate_pv_curtailment(0.1, (station, object()))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

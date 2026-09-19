"""分布式光伏群调群控的确定性场景回归套件。

这些场景用于验证控制策略，不代表正式96点优化结果或现场事件回放。每个
场景都保存完整输入、决策和事件，并以具名检查项给出可审计的通过判定。
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Callable

import numpy as np

from .analysis import write_summary
from .group_control import GroupControlSupervisor
from .hierarchy_types import (
    GroupControlAction,
    GroupControlConfig,
    GroupControlDecision,
    GroupControlEvent,
    GroupControlInput,
    GroupControlState,
)


@dataclass(frozen=True)
class GroupControlScenarioRecord:
    """一个场景判定时刻的输入和监督器输出。"""

    control_input: GroupControlInput
    decision: GroupControlDecision


@dataclass(frozen=True)
class GroupControlScenarioResult:
    """一个确定性场景的执行轨迹、验收检查和事件。"""

    name: str
    description: str
    records: tuple[GroupControlScenarioRecord, ...]
    events: tuple[GroupControlEvent, ...]
    checks: dict[str, bool]
    passed: bool

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name, passed in self.checks.items() if not passed)


class _ScenarioRecorder:
    """为场景构造连续时间戳并记录每次监督器调用。"""

    def __init__(self, config: GroupControlConfig) -> None:
        self.config = config
        self.supervisor = GroupControlSupervisor(config)
        self.records: list[GroupControlScenarioRecord] = []
        self.next_time_minutes = 0.0

    def step(self, **overrides: object) -> GroupControlDecision:
        values: dict[str, object] = {
            "time_minutes": self.next_time_minutes,
            "pcc_power_mw": 1.0,
            "current_net_load_mw": 8.0,
            "previous_net_load_mw": 8.0,
            "maximum_load_mw": 10.0,
            "pv_capacity_mw": 4.0,
            "reverse_flow_probability": 0.0,
            "p_grid_max_mw": 18.0,
            "pv_actual_mw": 2.0,
        }
        values.update(overrides)
        control_input = GroupControlInput(**values)  # type: ignore[arg-type]
        decision = self.supervisor.step(control_input)
        self.records.append(GroupControlScenarioRecord(control_input, decision))
        self.next_time_minutes = (
            float(control_input.time_minutes) + self.config.decision_interval_minutes
        )
        return decision


def _result(
    name: str,
    description: str,
    recorder: _ScenarioRecorder,
    checks: dict[str, bool],
) -> GroupControlScenarioResult:
    normalized = {key: bool(value) for key, value in checks.items()}
    return GroupControlScenarioResult(
        name=name,
        description=description,
        records=tuple(recorder.records),
        events=recorder.supervisor.events,
        checks=normalized,
        passed=all(normalized.values()),
    )


def _normal_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    decisions = [recorder.step() for _ in range(10)]
    return _result(
        "normal_operation",
        "PCC持续高于恢复阈值，不应产生限发、恢复或硬触发事件。",
        recorder,
        {
            "all_states_normal": all(
                decision.state is GroupControlState.NORMAL for decision in decisions
            ),
            "no_control_actions": all(
                decision.action is GroupControlAction.NONE for decision in decisions
            ),
            "no_audit_events": not recorder.supervisor.events,
        },
    )


def _emergency_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    decision = recorder.step(
        pcc_power_mw=0.0,
        current_net_load_mw=6.8,
        previous_net_load_mw=8.0,
    )
    return _result(
        "emergency_curtailment",
        "净负荷突降超过紧急阈值，第一次风险越限立即触发光伏限发。",
        recorder,
        {
            "curtailment_requested": decision.action is GroupControlAction.CURTAIL,
            "emergency_reason": decision.trigger_reason == "emergency_load_change_rate",
            "marked_immediate": decision.immediate,
            "positive_request": decision.requested_curtailment_mw > 0.0,
        },
    )


def _consecutive_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    decisions = [recorder.step(pcc_power_mw=0.0) for _ in range(3)]
    return _result(
        "consecutive_three_trigger",
        "风险限值连续越限三次，第三次触发普通限发。",
        recorder,
        {
            "first_two_debounced": all(
                decision.action is GroupControlAction.NONE for decision in decisions[:2]
            ),
            "third_curtails": decisions[-1].action is GroupControlAction.CURTAIL,
            "correct_reason": decisions[-1].trigger_reason == "consecutive_risk_limit",
            "not_emergency": not decisions[-1].immediate,
        },
    )


def _three_in_five_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    decisions = [
        recorder.step(pcc_power_mw=pcc)
        for pcc in (0.0, 1.0, 0.0, 1.0, 0.0)
    ]
    return _result(
        "three_in_five_debounce",
        "交替抖动的五次观测中有三次风险越限，仅在完整窗口末触发限发。",
        recorder,
        {
            "first_four_debounced": all(
                decision.action is GroupControlAction.NONE for decision in decisions[:4]
            ),
            "fifth_curtails": decisions[-1].action is GroupControlAction.CURTAIL,
            "correct_reason": decisions[-1].trigger_reason == "frequency_risk_limit",
            "window_has_three_hits": decisions[-1].observation_window_hits == 3,
        },
    )


def _secondary_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    first = recorder.step(
        pcc_power_mw=0.0,
        current_net_load_mw=6.8,
        previous_net_load_mw=8.0,
    )
    second = recorder.step(
        pcc_power_mw=0.0,
        measured_curtailment_mw=first.requested_curtailment_mw,
    )
    return _result(
        "secondary_curtailment",
        "首轮限发后PCC再次进入风险区，应绕过3/5等待立即二次限发。",
        recorder,
        {
            "initial_curtailment": first.action is GroupControlAction.CURTAIL,
            "secondary_curtailment": second.action is GroupControlAction.CURTAIL,
            "correct_reason": second.trigger_reason == "secondary_control",
            "secondary_is_immediate": second.immediate,
        },
    )


def _enter_recovery(
    recorder: _ScenarioRecorder,
) -> tuple[float, GroupControlDecision]:
    curtailed = recorder.step(
        pcc_power_mw=0.0,
        current_net_load_mw=6.8,
        previous_net_load_mw=8.0,
    )
    remaining = curtailed.requested_curtailment_mw
    recorder.step(pcc_power_mw=1.0, measured_curtailment_mw=remaining)
    restore = recorder.step(pcc_power_mw=1.0, measured_curtailment_mw=remaining)
    return remaining, restore


def _successful_recovery_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    remaining, restore = _enter_recovery(recorder)
    restoration_commands = 0
    evaluations_passed = 0
    expected_steps = int(ceil(1.0 / config.recovery_step_fraction))
    guard = 0
    while remaining > 1e-9 and guard < expected_steps + 2:
        guard += 1
        if restore.action is not GroupControlAction.RESTORE:
            break
        restoration_commands += 1
        requested = restore.requested_restoration_mw
        updated = max(0.0, remaining - requested)
        dwell_steps = (
            config.recovery_pause_minutes // config.decision_interval_minutes
        )
        for _ in range(dwell_steps - 1):
            recorder.step(pcc_power_mw=1.0, measured_curtailment_mw=updated)
        evaluation = recorder.step(
            pcc_power_mw=1.0,
            measured_curtailment_mw=updated,
            achieved_restoration_mw=requested,
        )
        evaluations_passed += int(evaluation.recovery_evaluation_passed is True)
        remaining = updated
        if remaining > 1e-9:
            restore = recorder.step(
                pcc_power_mw=1.0,
                measured_curtailment_mw=remaining,
            )
    final = recorder.records[-1].decision
    return _result(
        "successful_progressive_recovery",
        "每次恢复10%并完整驻留三分钟，响应匹配后最终解除全部限发。",
        recorder,
        {
            "expected_recovery_step_count": restoration_commands == expected_steps,
            "every_step_evaluated": evaluations_passed == restoration_commands,
            "remaining_curtailment_zero": final.remaining_curtailment_mw <= 1e-9,
            "returns_to_normal": final.state is GroupControlState.NORMAL,
            "no_recovery_abort": all(
                event.event_type != "recovery_abort"
                for event in recorder.supervisor.events
            ),
        },
    )


def _recovery_failure_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    remaining, restore = _enter_recovery(recorder)
    requested = restore.requested_restoration_mw
    dwell_steps = config.recovery_pause_minutes // config.decision_interval_minutes
    for _ in range(dwell_steps - 1):
        recorder.step(pcc_power_mw=1.0, measured_curtailment_mw=remaining)
    failed = recorder.step(
        pcc_power_mw=1.0,
        measured_curtailment_mw=remaining,
        achieved_restoration_mw=0.0,
    )
    return _result(
        "recovery_response_failure",
        "恢复指令驻留期满但设备未响应，监督器应保留限发量并中止恢复。",
        recorder,
        {
            "restore_was_requested": requested > 0.0,
            "recovery_aborted": failed.action is GroupControlAction.ABORT_RECOVERY,
            "correct_reason": failed.trigger_reason == "restoration_response_mismatch",
            "evaluation_failed": failed.recovery_evaluation_passed is False,
            "curtailment_retained": failed.remaining_curtailment_mw >= remaining - 1e-9,
        },
    )


def _communication_failure_scenario(config: GroupControlConfig) -> GroupControlScenarioResult:
    recorder = _ScenarioRecorder(config)
    curtailed = recorder.step(
        pcc_power_mw=0.0,
        current_net_load_mw=6.8,
        previous_net_load_mw=8.0,
    )
    remaining = curtailed.requested_curtailment_mw
    recorder.step(pcc_power_mw=1.0, measured_curtailment_mw=remaining)
    failed = recorder.step(
        pcc_power_mw=1.0,
        measured_curtailment_mw=remaining,
        recovery_command_acknowledged=False,
    )
    inhibited = recorder.step(
        pcc_power_mw=1.0,
        measured_curtailment_mw=remaining,
        recovery_command_acknowledged=True,
    )
    rearmed = recorder.step(
        pcc_power_mw=1.0,
        measured_curtailment_mw=remaining,
        recovery_command_acknowledged=True,
        recovery_rearm_requested=True,
    )
    retry_ready = recorder.step(
        pcc_power_mw=1.0,
        measured_curtailment_mw=remaining,
        recovery_command_acknowledged=True,
    )
    retried = recorder.step(
        pcc_power_mw=1.0,
        measured_curtailment_mw=remaining,
        recovery_command_acknowledged=True,
    )
    return _result(
        "communication_failure_and_reentry",
        "恢复指令未确认时锁存中止；通信恢复后仍需显式复归和重新准入。",
        recorder,
        {
            "communication_failure_aborts": (
                failed.action is GroupControlAction.ABORT_RECOVERY
            ),
            "correct_failure_reason": (
                failed.trigger_reason == "recovery_command_not_acknowledged"
            ),
            "recovery_failure_latched": (
                inhibited.state is GroupControlState.RECOVERY_INHIBIT
                and inhibited.action is GroupControlAction.HOLD
            ),
            "explicit_rearm_required": (
                rearmed.state is GroupControlState.CURTAILED_HOLD
                and rearmed.action is GroupControlAction.HOLD
            ),
            "reentry_requires_wait_state": retry_ready.state is GroupControlState.RESTORE_WAIT,
            "restoration_reissued_next_cycle": retried.action is GroupControlAction.RESTORE,
            "curtailment_preserved": retried.remaining_curtailment_mw >= remaining - 1e-9,
        },
    )


_SCENARIOS: tuple[
    Callable[[GroupControlConfig], GroupControlScenarioResult], ...
] = (
    _normal_scenario,
    _emergency_scenario,
    _consecutive_scenario,
    _three_in_five_scenario,
    _secondary_scenario,
    _successful_recovery_scenario,
    _recovery_failure_scenario,
    _communication_failure_scenario,
)


def run_group_control_scenarios(
    *,
    config: GroupControlConfig | None = None,
) -> dict[str, GroupControlScenarioResult]:
    """运行全部确定性群控场景；返回顺序稳定的结果映射。"""
    cfg = config or GroupControlConfig()
    results = [scenario(cfg) for scenario in _SCENARIOS]
    return {result.name: result for result in results}


def _record_row(
    scenario: GroupControlScenarioResult,
    record: GroupControlScenarioRecord,
) -> dict[str, object]:
    control_input = record.control_input
    decision = record.decision
    return {
        "scenario": scenario.name,
        "time_minute": control_input.time_minutes,
        "pcc_power_mw": control_input.pcc_power_mw,
        "current_net_load_mw": control_input.current_net_load_mw,
        "previous_net_load_mw": control_input.previous_net_load_mw,
        "measured_curtailment_mw": (
            "" if control_input.measured_curtailment_mw is None
            else control_input.measured_curtailment_mw
        ),
        "achieved_restoration_mw": (
            "" if control_input.achieved_restoration_mw is None
            else control_input.achieved_restoration_mw
        ),
        "recovery_command_acknowledged": control_input.recovery_command_acknowledged,
        "risk_limit_mw": decision.thresholds.risk_limit_mw,
        "safety_threshold_mw": decision.thresholds.safety_threshold_mw,
        "restore_threshold_mw": decision.thresholds.restore_threshold_mw,
        "load_change_rate": decision.thresholds.load_change_rate,
        "risk_index": decision.thresholds.risk_index,
        "state": decision.state.value,
        "action": decision.action.value,
        "reason": decision.trigger_reason,
        "required_curtailment_mw": decision.required_curtailment_mw,
        "requested_curtailment_mw": decision.requested_curtailment_mw,
        "unserved_curtailment_mw": decision.unserved_curtailment_mw,
        "requested_restoration_mw": decision.requested_restoration_mw,
        "remaining_curtailment_mw": decision.remaining_curtailment_mw,
        "recovery_dwell_remaining_minutes": decision.recovery_dwell_remaining_minutes,
        "recovery_evaluation_passed": (
            "" if decision.recovery_evaluation_passed is None
            else decision.recovery_evaluation_passed
        ),
        "restoration_response_error_mw": decision.restoration_response_error_mw,
    }


def write_group_control_scenario_outputs(
    output: Path,
    results: dict[str, GroupControlScenarioResult],
) -> None:
    """导出场景摘要、逐步轨迹、事件表和总览图。"""
    os.environ.setdefault("MPLCONFIGDIR", str(Path.cwd() / "results" / ".matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "purpose": (
            "deterministic group-control strategy regression; "
            "not a formal 96-step optimization result or field event replay"
        ),
        "all_passed": all(result.passed for result in results.values()),
        "scenario_count": len(results),
        "scenarios": {
            name: {
                "description": result.description,
                "passed": result.passed,
                "checks": result.checks,
                "failed_checks": list(result.failed_checks),
                "step_count": len(result.records),
                "event_count": len(result.events),
            }
            for name, result in results.items()
        },
    }
    write_summary(output / "summary.json", summary)

    timeseries_path = output / "scenario_timeseries.csv"
    rows = [
        _record_row(result, record)
        for result in results.values()
        for record in result.records
    ]
    if not rows:
        raise ValueError("at least one scenario record is required")
    with timeseries_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    event_fields = [
        "scenario", "time_minute", "event_type", "reason", "previous_state",
        "new_state", "requested_power_mw", "achieved_power_mw",
    ]
    with (output / "scenario_events.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=event_fields)
        writer.writeheader()
        for name, result in results.items():
            for event in result.events:
                writer.writerow({
                    "scenario": name,
                    "time_minute": event.time_minutes,
                    "event_type": event.event_type,
                    "reason": event.reason,
                    "previous_state": event.previous_state.value,
                    "new_state": event.new_state.value,
                    "requested_power_mw": event.requested_power_mw,
                    "achieved_power_mw": event.achieved_power_mw,
                })

    columns = 2
    rows_count = int(np.ceil(len(results) / columns))
    fig, axes = plt.subplots(rows_count, columns, figsize=(13, 3.2 * rows_count))
    axes_array = np.asarray(axes, dtype=object).reshape(-1)
    for ax, result in zip(axes_array, results.values()):
        time = np.asarray([
            record.control_input.time_minutes for record in result.records
        ])
        pcc = np.asarray([
            record.control_input.pcc_power_mw for record in result.records
        ])
        risk = np.asarray([
            record.decision.thresholds.risk_limit_mw for record in result.records
        ])
        safety = np.asarray([
            record.decision.thresholds.safety_threshold_mw for record in result.records
        ])
        restore = np.asarray([
            record.decision.thresholds.restore_threshold_mw for record in result.records
        ])
        actions = np.asarray([
            record.decision.action.value for record in result.records
        ])
        ax.plot(time, pcc, color="#263238", marker="o", markersize=3, label="PCC")
        ax.plot(time, risk, color="#d32f2f", label="risk")
        ax.plot(time, safety, color="#f57c00", label="safe")
        ax.plot(time, restore, color="#388e3c", label="restore")
        for action, marker, color in (
            ("curtail", "v", "#c62828"),
            ("restore", "^", "#2e7d32"),
            ("abort_recovery", "x", "#6a1b9a"),
        ):
            selected = actions == action
            if np.any(selected):
                ax.scatter(time[selected], pcc[selected], marker=marker, color=color, s=38, zorder=4, label=action)
        ax.set_title(f"{result.name} | {'PASS' if result.passed else 'FAIL'}")
        ax.set_xlabel("Minute")
        ax.set_ylabel("MW")
        ax.grid(alpha=0.22)
        ax.legend(fontsize=7, ncol=3)
    for ax in axes_array[len(results):]:
        ax.set_visible(False)
    fig.suptitle("Deterministic PV group-control scenario regression")
    fig.tight_layout()
    fig.savefig(output / "scenario_overview.png", dpi=160)
    plt.close(fig)


__all__ = [
    "GroupControlScenarioRecord",
    "GroupControlScenarioResult",
    "run_group_control_scenarios",
    "write_group_control_scenario_outputs",
]

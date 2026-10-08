"""Shared device evidence exports. No cluster planning or adoption rules."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Mapping

import numpy as np
from pydantic import TypeAdapter

from .hierarchy_types import DeviceTrackingResult
from .modules.control.contracts import (
    MinuteExecutionRecord,
    ResourcePowerSeries,
    StorageDynamicsTrajectory,
)


def write_minute_network_outputs(output: Path, tracking: Mapping[str, DeviceTrackingResult]) -> None:
    """Archive each causal phase, raw normalized injections, AC results and unknowns."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "storage_dynamics.json").write_text(
        TypeAdapter(dict[str, StorageDynamicsTrajectory])
        .dump_json(
            {
                name: StorageDynamicsTrajectory(
                    version="storage-dynamics-v1" if item.storage_dynamics else None,
                    complete=bool(item.storage_dynamics)
                    and len(item.storage_dynamics) == len(item.time_minutes),
                    records=item.storage_dynamics,
                )
                for name, item in tracking.items()
            },
            indent=2,
        )
        .decode("utf-8"),
        encoding="utf-8",
    )
    fields = ["microgrid", "time_minutes", "stage", "is_actual", "status", "valid", "reasons",
              "voltage_within_limits", "line_capacity_within_limits", "minimum_voltage_pu",
              "maximum_voltage_pu", "maximum_loading_pu", "pcc_import_mw", "pcc_reactive_mvar",
              "loss_mw", "recovery_blocked"]

    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(type(value).__name__)

    (output / "minute_network_context.json").write_text(json.dumps(
        {name: item.network_context for name, item in tracking.items()},
        ensure_ascii=False, indent=2, allow_nan=False, default=encode), encoding="utf-8")

    (output / "minute_execution.json").write_text(
        json.dumps(
            {
                name: asdict(
                    MinuteExecutionRecord(
                        version=item.execution_evidence_version,
                        time_minutes=tuple(float(v) for v in item.time_minutes),
                        wind_resource_actual_mw=tuple(
                            ResourcePowerSeries(rid, tuple(float(v) for v in values))
                            for rid, values in item.wind_resource_actual_mw.items()
                        ),
                        wind_availability_limited_mw=tuple(
                            float(v) for v in item.wind_availability_limited_mw
                        ),
                        pv_availability_limited_mw=tuple(
                            float(v) for v in item.pv_availability_limited_mw
                        ),
                        restoration_evidence=item.group_restoration_evidence,
                    )
                )
                for name, item in tracking.items()
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    with (output / "minute_network.jsonl").open("w", encoding="utf-8") as detail, \
         (output / "minute_network.csv").open("w", newline="", encoding="utf-8-sig") as table:
        writer = csv.DictWriter(table, fieldnames=fields)
        writer.writeheader()
        for name, item in tracking.items():
            blocked = dict(zip(item.time_minutes, item.network_recovery_blocked))
            for record in item.network_feedback:
                payload = dict(microgrid=name, **record)
                payload["recovery_blocked_at_end_of_step"] = bool(blocked[record["time_minutes"]])
                detail.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=encode) + "\n")
                flow = record["flow"] if record["valid"] else None
                row = {key: payload.get(key) for key in fields}
                row["recovery_blocked"] = payload["recovery_blocked_at_end_of_step"]
                row["reasons"] = ";".join(record["reasons"])
                if flow:
                    row.update(minimum_voltage_pu=min(b["voltage_pu"] for b in flow["buses"]),
                               maximum_voltage_pu=max(b["voltage_pu"] for b in flow["buses"]),
                               maximum_loading_pu=max((b["loading_pu"] for b in flow["branches"]), default=0.0),
                               pcc_import_mw=flow["pcc_import_mw"], pcc_reactive_mvar=flow["pcc_reactive_mvar"],
                               loss_mw=flow["loss_mw"])
                writer.writerow(row)


def _csv_optional_boolean(value: float) -> str | bool:
    """把三态恢复评价转换为CSV中的空值/布尔值。"""
    return "" if np.isnan(value) else bool(value)


def _write_group_control_timeseries(path: Path, tracking: Mapping[str, DeviceTrackingResult]) -> None:
    """导出逐分钟群控输入、决策、设备实绩和最后一级保护动作。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time_minute",
        "time_hour",
        "microgrid",
        "execution_evidence_version",
        "pcc_plan_mw",
        "pcc_before_local_safety_mw",
        "pcc_actual_mw",
        "risk_limit_mw",
        "safety_threshold_mw",
        "restore_threshold_mw",
        "net_load_feature_mw",
        "load_change_rate",
        "pv_penetration",
        "reverse_flow_probability",
        "observed_reverse_flow_probability",
        "reverse_flow_probability_valid",
        "risk_index",
        "state",
        "action",
        "reason",
        "pv_plan_mw",
        "pv_available_mw",
        "pv_control_target_mw",
        "pv_actual_mw",
        "required_curtailment_mw",
        "requested_curtailment_mw",
        "executable_curtailment_mw",
        "unserved_curtailment_mw",
        "measured_curtailment_mw",
        "remaining_curtailment_mw",
        "requested_restoration_mw",
        "achieved_restoration_mw",
        "recovery_dwell_remaining_minutes",
        "recovery_evaluation_passed",
        "restoration_response_error_mw",
        "storage_actual_mw",
        "storage_energy_mwh",
        "storage_soc_within_limits",
        "actual_power_factor",
        "local_reverse_flow_reduction_mw",
        "local_reverse_flow_action",
        "hard_override_active",
        "hard_wind_storage_target_mw",
        "hard_wind_storage_command_accepted",
        "hard_safety_unserved_mw",
        "wind_q_actual_mvar",
        "pv_q_actual_mvar",
        "storage_q_actual_mvar",
        "svg_q_actual_mvar",
        "reactive_unserved_mvar",
        "reactive_command_accepted",
        "reactive_execution_known",
        "local_power_factor_adjustment_mvar",
        "local_power_factor_action",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for name, item in tracking.items():
            executable = np.maximum(
                0.0,
                item.group_required_curtailment_mw
                - item.group_unserved_curtailment_mw,
            )
            for k, minute in enumerate(item.time_minutes):
                writer.writerow(
                    {
                        "time_minute": float(minute),
                        "execution_evidence_version": item.execution_evidence_version or "",
                        "time_hour": float(minute / 60.0),
                        "microgrid": name,
                        "pcc_plan_mw": float(item.pcc_command_mw[k]),
                        "pcc_before_local_safety_mw": float(item.pcc_before_local_safety_mw[k]),
                        "pcc_actual_mw": float(item.pcc_actual_mw[k]),
                        "risk_limit_mw": float(item.group_risk_limit_mw[k]),
                        "safety_threshold_mw": float(item.group_safety_threshold_mw[k]),
                        "restore_threshold_mw": float(item.group_restore_threshold_mw[k]),
                        "net_load_feature_mw": float(item.group_current_net_load_mw[k]),
                        "load_change_rate": float(item.group_load_change_rate[k]),
                        "pv_penetration": float(item.group_pv_penetration[k]),
                        "reverse_flow_probability": float(item.group_reverse_flow_probability[k]),
                        "observed_reverse_flow_probability": (
                            ""
                            if np.isnan(item.group_observed_reverse_flow_probability[k])
                            else float(item.group_observed_reverse_flow_probability[k])
                        ),
                        "reverse_flow_probability_valid": bool(
                            item.group_reverse_flow_probability_valid[k]
                        ),
                        "risk_index": float(item.group_risk_index[k]),
                        "state": str(item.group_control_state[k]),
                        "action": str(item.group_control_action[k]),
                        "reason": str(item.group_control_reason[k]),
                        "pv_plan_mw": float(item.pv_plan_mw[k]),
                        "pv_available_mw": float(item.pv_available_mw[k]),
                        "pv_control_target_mw": float(item.pv_control_target_mw[k]),
                        "pv_actual_mw": float(item.pv_actual_mw[k]),
                        "requested_curtailment_mw": float(item.group_requested_curtailment_mw[k]),
                        "required_curtailment_mw": float(item.group_required_curtailment_mw[k]),
                        "executable_curtailment_mw": float(executable[k]),
                        "unserved_curtailment_mw": float(item.group_unserved_curtailment_mw[k]),
                        "measured_curtailment_mw": float(item.measured_pv_curtailment_mw[k]),
                        "remaining_curtailment_mw": float(item.group_remaining_curtailment_mw[k]),
                        "requested_restoration_mw": float(item.group_requested_restoration_mw[k]),
                        "achieved_restoration_mw": (
                            float(item.group_achieved_restoration_mw[k])
                            if item.execution_evidence_version == "minute-execution-v2"
                            and np.isfinite(item.group_achieved_restoration_mw[k])
                            else ""
                        ),
                        "recovery_dwell_remaining_minutes": float(
                            item.group_recovery_dwell_remaining_minutes[k]
                        ),
                        "recovery_evaluation_passed": _csv_optional_boolean(
                            float(item.group_recovery_evaluation_passed[k])
                        ),
                        "restoration_response_error_mw": float(
                            item.group_restoration_response_error_mw[k]
                        ),
                        "storage_actual_mw": float(item.storage_actual_mw[k]),
                        "storage_energy_mwh": float(item.storage_energy_mwh[k]),
                        "storage_soc_within_limits": bool(item.storage_soc_within_limits[k]),
                        "actual_power_factor": float(item.actual_power_factor[k]),
                        "local_reverse_flow_reduction_mw": float(
                            item.local_reverse_flow_reduction_mw[k]
                        ),
                        "local_reverse_flow_action": bool(
                            item.local_reverse_flow_reduction_mw[k] > 1e-10
                        ),
                        "hard_override_active": bool(item.group_hard_override_active[k]),
                        "hard_wind_storage_target_mw": (
                            ""
                            if np.isnan(item.hard_wind_storage_target_mw[k])
                            else float(item.hard_wind_storage_target_mw[k])
                        ),
                        "hard_wind_storage_command_accepted": bool(
                            item.hard_wind_storage_command_accepted[k]
                        ),
                        "hard_safety_unserved_mw": float(item.hard_safety_unserved_mw[k]),
                        "wind_q_actual_mvar": float(item.wind_reactive_actual_mvar[k]),
                        "pv_q_actual_mvar": float(item.pv_reactive_actual_mvar[k]),
                        "storage_q_actual_mvar": float(item.storage_reactive_actual_mvar[k]),
                        "svg_q_actual_mvar": float(item.svg_reactive_actual_mvar[k]),
                        "reactive_unserved_mvar": float(item.reactive_dispatch_unserved_mvar[k]),
                        "reactive_command_accepted": bool(
                            item.shancheng_reactive_command_accepted[k]
                        ),
                        "reactive_execution_known": bool(item.reactive_execution_known[k]),
                        "local_power_factor_adjustment_mvar": float(
                            item.local_power_factor_adjustment_mvar[k]
                        ),
                        "local_power_factor_action": bool(
                            abs(item.local_power_factor_adjustment_mvar[k]) > 1e-10
                        ),
                    }
                )


def _write_group_control_events(path: Path, tracking: Mapping[str, DeviceTrackingResult]) -> None:
    """导出跨区域按时间排序的群控审计事件；无事件时仍写出表头。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time_minute", "time_hour", "microgrid", "event_type", "reason",
        "previous_state", "new_state", "requested_power_mw", "achieved_power_mw",
    ]
    events = sorted(
        (
            (event.time_minutes, name, event)
            for name, item in tracking.items()
            for event in item.group_control_events
        ),
        key=lambda value: (value[0], value[1]),
    )
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for minute, name, event in events:
            writer.writerow({
                "time_minute": float(minute),
                "time_hour": float(minute / 60.0),
                "microgrid": name,
                "event_type": event.event_type,
                "reason": event.reason,
                "previous_state": event.previous_state.value,
                "new_state": event.new_state.value,
                "requested_power_mw": event.requested_power_mw,
                "achieved_power_mw": event.achieved_power_mw,
            })


def _device_step_minutes(item: DeviceTrackingResult) -> float:
    if item.time_minutes.size < 2:
        return 1.0
    return float(np.median(np.diff(item.time_minutes)))


def _group_control_metrics(item: DeviceTrackingResult) -> dict[str, float | int | None]:
    """按统一口径汇总一个区域的群控验收指标。"""
    step_minutes = _device_step_minutes(item)
    curtailment_reasons = [
        event.reason
        for event in item.group_control_events
        if event.event_type == "curtailment_command"
    ]
    all_reasons = [event.reason for event in item.group_control_events]
    below_risk = item.pcc_before_local_safety_mw < item.group_risk_limit_mw
    pv_error = item.pv_actual_mw - item.pv_control_target_mw
    observed_commands = {
        e.command_id for e in item.group_restoration_evidence if e.status == "observed"
    }
    incomplete_restorations = max(0, item.group_restoration_commands - len(observed_commands))
    return {
        "risk_limit_violation_count": int(np.count_nonzero(below_risk)),
        "risk_limit_violation_duration_minutes": float(np.count_nonzero(below_risk) * step_minutes),
        "emergency_trigger_count": curtailment_reasons.count("emergency_load_change_rate"),
        "consecutive_three_trigger_count": curtailment_reasons.count("consecutive_risk_limit"),
        "three_in_five_trigger_count": curtailment_reasons.count("frequency_risk_limit"),
        "secondary_control_count": curtailment_reasons.count("secondary_control"),
        "requested_curtailment_mw_sum": float(np.sum(item.group_requested_curtailment_mw)),
        "unserved_curtailment_mw_sum": float(np.sum(item.group_unserved_curtailment_mw)),
        "measured_curtailment_energy_mwh": float(
            np.sum(item.measured_pv_curtailment_mw) * step_minutes / 60.0
        ),
        "requested_restoration_mw_sum": float(np.sum(item.group_requested_restoration_mw)),
        "achieved_restoration_mw_sum": (
            float(np.nansum(item.group_achieved_restoration_mw))
            if item.execution_evidence_version == "minute-execution-v2"
            and incomplete_restorations == 0
            else None
        ),
        "restoration_unknown_count": sum(
            e.status == "unknown" for e in item.group_restoration_evidence
        ),
        "restoration_incomplete_count": incomplete_restorations,
        "recovery_completion_count": all_reasons.count("recovery_completed"),
        "recovery_abort_count": item.group_recovery_aborts,
        "local_reverse_flow_intervention_count": item.local_reverse_flow_interventions,
        "hard_wind_storage_command_count": int(
            np.count_nonzero(item.hard_wind_storage_command_accepted)
        ),
        "hard_safety_unserved_mw_sum": float(np.sum(item.hard_safety_unserved_mw)),
        "hard_safety_unserved_mw_max": float(
            np.max(
                item.hard_safety_unserved_mw,
                initial=0.0,
            )
        ),
        "local_power_factor_intervention_count": item.local_power_factor_interventions,
        "pv_control_tracking_rmse_mw": float(np.sqrt(np.mean(pv_error**2))),
    }

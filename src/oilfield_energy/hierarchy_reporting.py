"""多层级控制结果的结构化导出与绘图。"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

os.environ.setdefault("MPLCONFIGDIR", str(Path.cwd() / "results" / ".matplotlib"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pydantic import TypeAdapter

from .ac_power_flow import backward_forward_sweep
from .analysis import write_summary
from .data import ProjectCase
from .hierarchy_types import ADMMResult, DeviceTrackingResult, HierarchicalResult
from .modules.control.contracts import (
    MinuteExecutionRecord,
    ResourcePowerSeries,
    StorageDynamicsTrajectory,
)
from .network_model import validate_legacy_network_alignment


def _write_admm_history(path: Path, result: ADMMResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "communication_tick", "primal_residual", "dual_residual",
        "primal_tolerance", "dual_tolerance", "fresh_region_count",
        "convergence_streak", "fallback_regions", "local_objective_cny",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in result.history:
            writer.writerow({
                "communication_tick": item.iteration,
                "primal_residual": item.primal_residual,
                "dual_residual": item.dual_residual,
                "primal_tolerance": item.primal_tolerance,
                "dual_tolerance": item.dual_tolerance,
                "fresh_region_count": item.fresh_region_count,
                "convergence_streak": item.convergence_streak,
                "fallback_regions": ";".join(item.fallback_regions),
                "local_objective_cny": item.local_objective_cny,
            })


def _write_regional_targets(
    path: Path,
    case: ProjectCase,
    names: Sequence[str],
    result: HierarchicalResult,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time_hour", "microgrid", "admm_p_reference_mw", "admm_q_reference_mvar",
        "day_ahead_realized_p_mw", "day_ahead_realized_q_mvar",
        "intraday_realized_p_mw", "intraday_realized_q_mvar",
        "centralized_p_mw", "centralized_q_mvar",
        "day_ahead_security_floor_mw", "intraday_security_floor_mw",
        "day_ahead_reverse_flow_probability", "intraday_reverse_flow_probability",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for name in names:
            day = result.local_milp_results[name].microgrids[name]
            intra = result.intraday_milp_results[name].microgrids[name]
            central = result.centralized_reference.microgrids[name]
            for t, hour in enumerate(case.time_hours):
                writer.writerow({
                    "time_hour": float(hour),
                    "microgrid": name,
                    "admm_p_reference_mw": float(result.admm.p_references_mw[name][t]),
                    "admm_q_reference_mvar": float(result.admm.q_references_mvar[name][t]),
                    "day_ahead_realized_p_mw": float(day["p_grid_mw"][t]),
                    "day_ahead_realized_q_mvar": float(day["q_grid_mvar"][t]),
                    "intraday_realized_p_mw": float(intra["p_grid_mw"][t]),
                    "intraday_realized_q_mvar": float(intra["q_grid_mvar"][t]),
                    "centralized_p_mw": float(central["p_grid_mw"][t]),
                    "centralized_q_mvar": float(central["q_grid_mvar"][t]),
                    "day_ahead_security_floor_mw": float(
                        result.day_ahead_security[name].effective_floor_mw[t]
                    ),
                    "intraday_security_floor_mw": float(
                        result.intraday_security[name].effective_floor_mw[t]
                    ),
                    "day_ahead_reverse_flow_probability": float(
                        result.day_ahead_security[name].reverse_flow_probability[t]
                    ),
                    "intraday_reverse_flow_probability": float(
                        result.intraday_security[name].reverse_flow_probability[t]
                    ),
                })


def _write_device_tracking(path: Path, result: HierarchicalResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time_minute", "microgrid", "p_command_mw", "p_actual_mw",
        "q_command_mvar", "q_actual_mvar", "wind_q_actual_mvar",
        "pv_q_actual_mvar", "storage_q_actual_mvar", "svg_q_actual_mvar",
        "reactive_unserved_mvar", "reactive_command_accepted",
        "reactive_execution_known", "network_recovery_blocked",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for name, item in result.tracking.items():
            for k, minute in enumerate(item.time_minutes):
                writer.writerow({
                    "time_minute": float(minute),
                    "microgrid": name,
                    "p_command_mw": float(item.pcc_command_mw[k]),
                    "p_actual_mw": float(item.pcc_actual_mw[k]),
                    "q_command_mvar": float(item.qcc_command_mvar[k]),
                    "q_actual_mvar": float(item.qcc_actual_mvar[k]),
                    "wind_q_actual_mvar": float(item.wind_reactive_actual_mvar[k]),
                    "pv_q_actual_mvar": float(item.pv_reactive_actual_mvar[k]),
                    "storage_q_actual_mvar": float(
                        item.storage_reactive_actual_mvar[k]
                    ),
                    "svg_q_actual_mvar": float(item.svg_reactive_actual_mvar[k]),
                    "reactive_unserved_mvar": float(
                        item.reactive_dispatch_unserved_mvar[k]
                    ),
                    "reactive_command_accepted": bool(
                        item.shancheng_reactive_command_accepted[k]
                    ),
                    "reactive_execution_known": bool(
                        item.reactive_execution_known[k]
                    ),
                    "network_recovery_blocked": bool(item.network_recovery_blocked[k]),
                })


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


def _write_group_control_timeseries(path: Path, result: HierarchicalResult) -> None:
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
        for name, item in result.tracking.items():
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


def _write_group_control_events(path: Path, result: HierarchicalResult) -> None:
    """导出跨区域按时间排序的群控审计事件；无事件时仍写出表头。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time_minute", "time_hour", "microgrid", "event_type", "reason",
        "previous_state", "new_state", "requested_power_mw", "achieved_power_mw",
    ]
    events = sorted(
        (
            (event.time_minutes, name, event)
            for name, item in result.tracking.items()
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


def _write_network_losses(
    path: Path,
    case: ProjectCase,
    names: Sequence[str],
    result: HierarchicalResult,
) -> None:
    """导出集中式MISOCP与独立AC的逐线路P/Q损耗对照。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lookup = {mg.name: mg for mg in case.microgrids}
    fields = [
        "time_hour", "microgrid", "line", "misocp_p_mw", "misocp_q_mvar",
        "branch_kind", "tap_ratio",
        "misocp_current_squared_mva2", "misocp_p_loss_mw", "misocp_q_loss_mvar",
        "ac_p_loss_mw", "ac_q_loss_mvar", "p_loss_difference_mw",
        "q_loss_difference_mvar", "cone_relative_gap",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for name in names:
            mg = lookup[name]
            lines = validate_legacy_network_alignment(mg).branches
            dispatch = result.centralized_reference.microgrids[name]
            p_bus = np.asarray(dispatch["bus_p_demand_mw"])
            q_bus = np.asarray(dispatch["bus_q_demand_mvar"])
            for t, hour in enumerate(case.time_hours):
                ac = backward_forward_sweep(mg, p_bus[:, t], q_bus[:, t])
                for idx, line in enumerate(lines):
                    p_model = float(dispatch["line_p_loss_mw"][idx, t])
                    q_model = float(dispatch["line_q_loss_mvar"][idx, t])
                    p_ac = float(ac["line_p_loss_mw"][idx])
                    q_ac = float(ac["line_q_loss_mvar"][idx])
                    writer.writerow({
                        "time_hour": float(hour),
                        "microgrid": name,
                        "line": line.name,
                        "branch_kind": line.kind.value,
                        "tap_ratio": line.tap_ratio,
                        "misocp_p_mw": float(dispatch["line_p_mw"][idx, t]),
                        "misocp_q_mvar": float(dispatch["line_q_mvar"][idx, t]),
                        "misocp_current_squared_mva2": float(dispatch["line_current_squared_mva2"][idx, t]),
                        "misocp_p_loss_mw": p_model,
                        "misocp_q_loss_mvar": q_model,
                        "ac_p_loss_mw": p_ac,
                        "ac_q_loss_mvar": q_ac,
                        "p_loss_difference_mw": p_model - p_ac,
                        "q_loss_difference_mvar": q_model - q_ac,
                        "cone_relative_gap": float(dispatch["cone_relative_gap"][idx, t]),
                    })


def _plot_admm(path: Path, result: ADMMResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray([item.iteration for item in result.history])
    primal = np.maximum([item.primal_residual for item in result.history], 1e-12)
    dual = np.maximum([item.dual_residual for item in result.history], 1e-12)
    eps_p = np.maximum([item.primal_tolerance for item in result.history], 1e-12)
    eps_d = np.maximum([item.dual_tolerance for item in result.history], 1e-12)
    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.semilogy(x, primal, label="primal residual", linewidth=1.8)
    ax.semilogy(x, dual, label="dual residual", linewidth=1.8)
    ax.semilogy(x, eps_p, "--", label="primal tolerance")
    ax.semilogy(x, eps_d, "--", label="dual tolerance")
    ax.set_xlabel("Communication tick")
    ax.set_ylabel("Residual norm")
    ax.set_title("Consensus ADMM convergence")
    ax.grid(alpha=0.25, which="both")
    ax.legend(ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_regional_targets(
    path: Path,
    case: ProjectCase,
    names: Sequence[str],
    result: HierarchicalResult,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(len(names), 1, figsize=(11, 3.1 * len(names)), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, name in zip(axes, names):
        day = result.local_milp_results[name].microgrids[name]
        central = result.centralized_reference.microgrids[name]
        ax.plot(case.time_hours, result.admm.p_references_mw[name], label="ADMM P reference", linewidth=2)
        ax.plot(case.time_hours, day["p_grid_mw"], "--", label="regional MISOCP realization")
        ax.plot(case.time_hours, central["p_grid_mw"], ":", label="centralized MISOCP")
        ax.set_ylabel(f"{name} (MW)")
        ax.grid(alpha=0.25)
        ax.legend(ncol=3)
    axes[-1].set_xlabel("Hour")
    fig.suptitle("Cluster references and independent regional realization")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_device_tracking(path: Path, result: HierarchicalResult) -> None:
    names = list(result.tracking)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(len(names), 1, figsize=(11, 3.1 * len(names)), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, name in zip(axes, names):
        item = result.tracking[name]
        ax.plot(item.time_minutes / 60.0, item.pcc_command_mw, label="PCC command", linewidth=1.5)
        ax.plot(item.time_minutes / 60.0, item.pcc_actual_mw, label="device response", linewidth=1.0)
        ax.set_ylabel(f"{name} (MW)")
        ax.grid(alpha=0.25)
        ax.legend(ncol=2)
    axes[-1].set_xlabel("Hour")
    fig.suptitle("One-minute device-layer tracking")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_group_control_thresholds(path: Path, result: HierarchicalResult) -> None:
    """绘制各区域PCC实际功率、三重阈值与限发/恢复动作。"""
    names = list(result.tracking)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(len(names), 1, figsize=(12, 3.4 * len(names)), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, name in zip(axes, names):
        item = result.tracking[name]
        hours = item.time_minutes / 60.0
        ax.plot(hours, item.pcc_actual_mw, color="#263238", linewidth=1.0, label="PCC actual")
        ax.plot(hours, item.group_risk_limit_mw, color="#d32f2f", linewidth=1.2, label="risk limit")
        ax.plot(hours, item.group_safety_threshold_mw, color="#f57c00", linewidth=1.2, label="safety threshold")
        ax.plot(hours, item.group_restore_threshold_mw, color="#388e3c", linewidth=1.2, label="restore threshold")
        curtail = item.group_control_action == "curtail"
        restore = item.group_control_action == "restore"
        if np.any(curtail):
            ax.scatter(hours[curtail], item.pcc_actual_mw[curtail], color="#c62828", marker="v", s=28, label="curtail command", zorder=4)
        if np.any(restore):
            ax.scatter(hours[restore], item.pcc_actual_mw[restore], color="#2e7d32", marker="^", s=28, label="restore command", zorder=4)
        ax.set_ylabel(f"{name} (MW)")
        ax.grid(alpha=0.22)
        ax.legend(ncol=3, fontsize=8, loc="upper right")
    axes[-1].set_xlabel("Hour")
    fig.suptitle("PV group-control PCC thresholds and actions")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_pv_curtailment_and_recovery(path: Path, result: HierarchicalResult) -> None:
    """绘制光伏计划、群控目标、实际响应及反事实口径限发量。"""
    names = list(result.tracking)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(len(names), 1, figsize=(12, 3.5 * len(names)), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, name in zip(axes, names):
        item = result.tracking[name]
        hours = item.time_minutes / 60.0
        ax.plot(hours, item.pv_plan_mw, color="#607d8b", linewidth=1.0, label="PV plan")
        ax.plot(hours, item.pv_control_target_mw, color="#ef6c00", linewidth=1.1, label="control target")
        ax.plot(hours, item.pv_actual_mw, color="#1565c0", linewidth=1.0, label="PV actual")
        ax.fill_between(
            hours,
            0.0,
            item.measured_pv_curtailment_mw,
            color="#e53935",
            alpha=0.22,
            label="measured curtailment",
        )
        restore = item.group_requested_restoration_mw > 1e-10
        if np.any(restore):
            ax.scatter(hours[restore], item.pv_actual_mw[restore], color="#2e7d32", marker="^", s=28, label="restore step", zorder=4)
        ax.set_ylabel(f"{name} (MW)")
        ax.grid(alpha=0.22)
        ax.legend(ncol=4, fontsize=8, loc="upper right")
    axes[-1].set_xlabel("Hour")
    fig.suptitle("PV curtailment allocation and progressive recovery")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


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


def _plot_loss_model_comparison(path: Path, result: HierarchicalResult) -> None:
    comparison = result.comparison
    labels = ["Legacy model", "Legacy dispatch\nAC", "MISOCP model", "MISOCP dispatch\nAC"]
    values = [
        float(comparison["legacy_model_active_loss_mwh"]),
        float(comparison["legacy_ac_active_loss_mwh"]),
        float(comparison["misocp_model_active_loss_mwh"]),
        float(comparison["misocp_ac_active_loss_mwh"]),
    ]
    colors = ["#9aa0a6", "#d97563", "#4f81bd", "#4fa36c"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 5.5))
    bars = ax.bar(labels, values, color=colors)
    ax.bar_label(bars, fmt="%.4f", padding=4)
    ax.set_ylabel("24-hour active energy loss (MWh)")
    ax.set_title("Legacy LinDistFlow approximation vs AC-consistent MISOCP")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_hierarchical_outputs(
    output: Path,
    case: ProjectCase,
    names: Sequence[str],
    result: HierarchicalResult,
    *,
    configuration: Mapping[str, object],
) -> None:
    """写出正常工况的摘要、逐时数据与关键图。"""
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "case_notice": "全部网络与运行数据均为参数化模拟数据，不代表实际油田数据。",
        "cluster_validation": asdict(result.cluster_validation)
        if result.cluster_validation
        else None,
        "coordination_snapshot": asdict(result.admm.coordination_snapshot)
        if result.admm.coordination_snapshot
        else None,
        "architecture": "AC-loss-calibrated convex consensus ADMM + independent AC-consistent regional MISOCP + centralized MISOCP benchmark",
        "configuration": dict(configuration),
        "comparison": result.comparison,
        "planning_security": {
            horizon: {
                name: {
                    "minimum_effective_floor_mw": float(np.min(item.effective_floor_mw)),
                    "maximum_effective_floor_mw": float(np.max(item.effective_floor_mw)),
                    "mean_reverse_flow_probability": float(np.mean(item.reverse_flow_probability)),
                    "maximum_risk_index": float(np.max(item.risk_index)),
                }
                for name, item in trajectories.items()
            }
            for horizon, trajectories in {
                "day_ahead": result.day_ahead_security,
                "intraday": result.intraday_security,
            }.items()
        },
        "admm": {
            "converged": result.admm.converged,
            "stop_reason": result.admm.stop_reason,
            "communication_ticks": result.admm.iterations,
            "coordination_updates": result.admm.coordination_updates,
            "final_iteration": asdict(result.admm.history[-1]),
            "communication": asdict(result.admm.communication),
            "peak_aggregate_import_mw": float(np.max(result.admm.aggregate_import_mw)),
        },
        "ac_consistency": {
            "centralized": {
                "passed": bool(result.centralized_ac_consistency.passed),
                "stop_reason": result.centralized_ac_consistency.stop_reason,
                "iterations": result.centralized_ac_consistency.iterations,
                "history": [asdict(item) for item in result.centralized_ac_consistency.history],
            },
            "regional": {
                name: {
                    "passed": bool(item.passed),
                    "stop_reason": item.stop_reason,
                    "iterations": item.iterations,
                    "history": [asdict(entry) for entry in item.history],
                }
                for name, item in result.local_ac_consistency.items()
            },
            "intraday": {
                name: {
                    "passed": bool(item.passed),
                    "stop_reason": item.stop_reason,
                    "iterations": item.iterations,
                    "history": [asdict(entry) for entry in item.history],
                }
                for name, item in result.intraday_ac_consistency.items()
            },
        },
        "centralized_misocp": {
            "solver_status": result.centralized_reference.message,
            "mip_gap": result.centralized_reference.mip_gap,
            "model_size": result.centralized_reference.model_size,
            "active_loss_mwh": float(result.centralized_reference.cluster["total_active_loss_mwh"]),
            "reactive_loss_mvarh": float(
                result.centralized_reference.cluster["total_reactive_loss_mvarh"]
            ),
            "maximum_cone_relative_gap": float(
                result.centralized_reference.cluster["maximum_cone_relative_gap"]
            ),
            "storage_binary_relaxation_used": bool(
                result.centralized_reference.cluster["storage_binary_relaxation_used"]
            ),
            "storage_integrality_certified": bool(
                result.centralized_reference.cluster["storage_integrality_certified"]
            ),
            "maximum_simultaneous_storage_mw": float(
                result.centralized_reference.cluster["maximum_simultaneous_storage_mw"]
            ),
        },
        "loss_model_comparison": {
            "legacy_lin_distflow_model_loss_mwh": result.comparison["legacy_model_active_loss_mwh"],
            "legacy_dispatch_ac_loss_mwh": result.comparison["legacy_ac_active_loss_mwh"],
            "legacy_loss_underestimate_percent": result.comparison[
                "legacy_loss_underestimate_percent"
            ],
            "misocp_model_loss_mwh": result.comparison["misocp_model_active_loss_mwh"],
            "misocp_dispatch_ac_loss_mwh": result.comparison["misocp_ac_active_loss_mwh"],
            "misocp_loss_relative_error_percent": result.comparison[
                "misocp_loss_relative_error_percent"
            ],
            "ac_loss_reduction_vs_legacy_dispatch_percent": result.comparison[
                "ac_loss_reduction_vs_legacy_dispatch_percent"
            ],
        },
        "regional_misocp": {
            name: {
                "solver_success": bool(result.local_milp_results[name].success),
                "economic_cost_cny": float(
                    result.local_milp_results[name].microgrids[name]["economic_cost_cny"]
                ),
                "p_tracking_rmse_mw": float(
                    result.local_milp_results[name].microgrids[name]["p_tracking_rmse_mw"]
                ),
                "q_tracking_rmse_mvar": float(
                    result.local_milp_results[name].microgrids[name]["q_tracking_rmse_mvar"]
                ),
            }
            for name in names
        },
        "device_layer": {
            name: {
                "execution_evidence_version": item.execution_evidence_version,
                "p_tracking_rmse_mw": item.p_tracking_rmse_mw
                if np.isfinite(item.p_tracking_rmse_mw)
                else None,
                "q_tracking_rmse_mvar": item.q_tracking_rmse_mvar
                if np.isfinite(item.q_tracking_rmse_mvar)
                else None,
                "no_reverse_violations_before_safety": item.no_reverse_violations_before_safety,
                "network_security_passed": item.network_security_passed,
                "network_invalid_steps": item.network_invalid_steps,
                "network_violation_steps": item.network_violation_steps,
                "no_reverse_violations_after_safety": item.no_reverse_violations_after_safety,
                "pf_violations_after_safety": item.pf_violations_after_safety,
                "safety_interventions": item.safety_interventions,
                "group_curtailment_commands": item.group_curtailment_commands,
                "group_restoration_commands": item.group_restoration_commands,
                "group_recovery_aborts": item.group_recovery_aborts,
                "local_reverse_flow_interventions": (item.local_reverse_flow_interventions),
                "hard_wind_storage_command_count": int(
                    np.count_nonzero(item.hard_wind_storage_command_accepted)
                ),
                "hard_safety_unserved_mw_sum": float(np.sum(item.hard_safety_unserved_mw)),
                "local_power_factor_interventions": (item.local_power_factor_interventions),
            }
            for name, item in result.tracking.items()
        },
        "group_control_metrics": {
            name: _group_control_metrics(item) for name, item in result.tracking.items()
        },
    }
    write_summary(output / "summary.json", summary)
    _write_admm_history(output / "admm_history.csv", result.admm)
    _write_regional_targets(output / "regional_targets.csv", case, names, result)
    _write_device_tracking(output / "device_tracking.csv", result)
    write_minute_network_outputs(output, result.tracking)
    _write_group_control_timeseries(output / "group_control_timeseries.csv", result)
    _write_group_control_events(output / "group_control_events.csv", result)
    _write_network_losses(output / "network_losses.csv", case, names, result)
    _plot_admm(output / "admm_convergence.png", result.admm)
    _plot_regional_targets(output / "regional_target_tracking.png", case, names, result)
    _plot_device_tracking(output / "device_tracking.png", result)
    _plot_group_control_thresholds(output / "group_control_thresholds.png", result)
    _plot_pv_curtailment_and_recovery(
        output / "pv_curtailment_and_recovery.png",
        result,
    )
    _plot_loss_model_comparison(output / "loss_model_comparison.png", result)


def write_fault_demo_outputs(
    output: Path,
    case: ProjectCase,
    result: ADMMResult,
    *,
    configuration: Mapping[str, object] | None = None,
) -> None:
    """写出通信故障回归算例，不将缩小算例混同为正式96时段结果。"""
    output.mkdir(parents=True, exist_ok=True)
    final = result.history[-1]
    write_summary(output / "summary.json", {
        "purpose": "reduced-size communication-fault regression; not the formal 96-step operational result",
        "steps": len(case.time_hours),
        "configuration": dict(configuration or {}),
        "converged_after_recovery": result.converged,
        "stop_reason": result.stop_reason,
        "communication_ticks": result.iterations,
        "coordination_updates": result.coordination_updates,
        "communication": asdict(result.communication),
        "final_primal_residual": final.primal_residual,
        "final_dual_residual": final.dual_residual,
        "final_primal_tolerance": final.primal_tolerance,
        "final_dual_tolerance": final.dual_tolerance,
        "peak_aggregate_import_mw": float(np.max(result.aggregate_import_mw)),
        "cluster_import_limit_mw": case.cluster_import_limit_mw,
    })
    _write_admm_history(output / "admm_history.csv", result)
    _plot_admm(output / "admm_convergence.png", result)

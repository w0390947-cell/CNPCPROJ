"""多层级控制系统总编排：日前ADMM、日内MISOCP、分钟级设备跟踪。"""

from __future__ import annotations

from typing import Dict, Sequence

import numpy as np

from .ac_consistency import ACConsistencyResult, solve_case_ac_consistent
from .ac_power_flow import validate_ac_dispatch
from .admm import run_admm_coordination
from .data import ProjectCase
from .device_control import simulate_device_tracking
from .hierarchy_types import (
    ADMMConfig,
    CommunicationConfig,
    DeviceTrackingResult,
    GroupControlConfig,
    HierarchicalResult,
    TimeScaleConfig,
)
from .model import OptimizationResult
from .model import solve_case as solve_case_legacy
from .modules.control.api import execution_substeps
from .modules.power_flow.api import assess_cluster_import, summarize_cluster_validation
from .modules.power_flow.contracts import RegionalImport
from .planning_security import build_planning_security_trajectories
from .scenarios import build_intraday_updated_case


def _realize_targets(
    case: ProjectCase,
    names: Sequence[str],
    p_references: Dict[str, np.ndarray],
    q_references: Dict[str, np.ndarray],
    time_limit_seconds: float,
    p_grid_security_floors_mw: Dict[str, np.ndarray],
) -> tuple[Dict[str, OptimizationResult], Dict[str, ACConsistencyResult]]:
    """每个区域单独构建并求解本地MISOCP，不共享内部变量。"""
    results: Dict[str, OptimizationResult] = {}
    consistency: Dict[str, ACConsistencyResult] = {}
    for name in names:
        target = {
            name: {
                "p_mw": np.asarray(p_references[name]),
                "q_mvar": np.asarray(q_references[name]),
            }
        }
        checked = solve_case_ac_consistent(
            case,
            [name],
            storage_enabled=True,
            cluster_coordination=False,
            pcc_targets=target,
            time_limit_seconds=time_limit_seconds,
            p_grid_security_floors_mw={name: p_grid_security_floors_mw[name]},
        )
        result = checked.optimization
        if not result.success:
            raise RuntimeError(f"regional MISOCP {name} failed: {result.message}")
        if not checked.passed:
            raise RuntimeError(
                f"regional MISOCP {name} failed AC consistency: {checked.stop_reason}"
            )
        results[name] = result
        consistency[name] = checked
    return results, consistency


def _combined_ac_pass(case: ProjectCase, results: Dict[str, OptimizationResult]) -> bool:
    return all(
        bool(validate_ac_dispatch(case, result, [name])["passed"])
        for name, result in results.items()
    )


def run_hierarchical_control(
    case: ProjectCase,
    microgrid_names: Sequence[str] | None = None,
    *,
    admm_config: ADMMConfig | None = None,
    communication_config: CommunicationConfig | None = None,
    time_scale_config: TimeScaleConfig | None = None,
    group_control_config: GroupControlConfig | None = None,
    time_limit_seconds: float = 180.0,
) -> HierarchicalResult:
    """运行完整三层仿真。

    1. 日前层：AC一致集中式MISOCP基准与凸ADMM协调；
    2. 日内层：更新预测后，各区域独立MISOCP跟踪P/Q参考；
    3. 设备层：1分钟响应、扰动和本地安全保护。
    """
    names = (
        list(microgrid_names)
        if microgrid_names is not None
        else [mg.name for mg in case.microgrids]
    )
    cfg_time = time_scale_config or TimeScaleConfig()
    execution_substeps(case.assumptions.dt_hours * 60, cfg_time.device_step_minutes)
    cfg_group = group_control_config or GroupControlConfig()
    day_ahead_security = build_planning_security_trajectories(
        case,
        names,
        config=cfg_group,
    )
    day_ahead_floors = {
        name: trajectory.effective_floor_mw for name, trajectory in day_ahead_security.items()
    }

    centralized_checked = solve_case_ac_consistent(
        case,
        names,
        storage_enabled=True,
        cluster_coordination=True,
        time_limit_seconds=time_limit_seconds,
        p_grid_security_floors_mw=day_ahead_floors,
    )
    centralized = centralized_checked.optimization
    if not centralized.success:
        raise RuntimeError(f"centralized reference failed: {centralized.message}")
    if not centralized_checked.passed:
        raise RuntimeError(
            f"centralized MISOCP failed AC consistency: {centralized_checked.stop_reason}"
        )

    # 保留同输入旧MILP用于量化“近似网损—AC网损”误差，不参与正式指令下发。
    legacy_centralized = solve_case_legacy(
        case,
        names,
        storage_enabled=True,
        cluster_coordination=True,
        time_limit_seconds=time_limit_seconds,
    )
    if not legacy_centralized.success:
        raise RuntimeError(f"legacy centralized comparison failed: {legacy_centralized.message}")
    legacy_ac_validation = validate_ac_dispatch(case, legacy_centralized, names)

    # 将AC一致Branch Flow逐时P/Q损耗反馈到凸ADMM区域聚合模型。
    loss_calibration = {
        name: {
            "p_loss_mw": np.asarray(centralized.microgrids[name]["loss_mw"]),
            "q_loss_mvar": np.asarray(centralized.microgrids[name]["reactive_loss_mvar"]),
        }
        for name in names
    }

    admm = run_admm_coordination(
        case,
        names,
        admm_config=admm_config,
        communication_config=communication_config,
        loss_calibration=loss_calibration,
        p_grid_security_floors_mw=day_ahead_floors,
        group_control_config=cfg_group,
    )
    local_results, local_consistency = _realize_targets(
        case,
        names,
        admm.p_references_mw,
        admm.q_references_mvar,
        time_limit_seconds,
        day_ahead_floors,
    )

    intraday_case = build_intraday_updated_case(case)
    intraday_security = build_planning_security_trajectories(
        intraday_case,
        names,
        config=cfg_group,
    )
    intraday_floors = {
        name: trajectory.effective_floor_mw for name, trajectory in intraday_security.items()
    }
    intraday_results, intraday_consistency = _realize_targets(
        intraday_case,
        names,
        admm.p_references_mw,
        admm.q_references_mvar,
        time_limit_seconds,
        intraday_floors,
    )
    tracking: Dict[str, DeviceTrackingResult] = {}
    intraday_lookup = {mg.name: mg for mg in intraday_case.microgrids}
    for idx, name in enumerate(names):
        tracking[name] = simulate_device_tracking(
            intraday_case,
            intraday_lookup[name],
            intraday_results[name],
            config=cfg_time,
            group_control_config=cfg_group,
            reverse_flow_probability_15=(intraday_security[name].reverse_flow_probability),
            seed=20260901 + idx,
        )

    centralized_cost = float(centralized.cluster["economic_cost_cny"])
    distributed_cost = float(
        sum(
            float(result.microgrids[name]["economic_cost_cny"])
            for name, result in local_results.items()
        )
    )
    intraday_cost = float(
        sum(
            float(result.microgrids[name]["economic_cost_cny"])
            for name, result in intraday_results.items()
        )
    )
    realized_p = np.vstack(
        [np.asarray(local_results[name].microgrids[name]["p_grid_mw"]) for name in names]
    )
    realized_q = np.vstack(
        [np.asarray(local_results[name].microgrids[name]["q_grid_mvar"]) for name in names]
    )
    target_p = np.vstack([admm.p_references_mw[name] for name in names])
    target_q = np.vstack([admm.q_references_mvar[name] for name in names])
    objective_gap = 100.0 * (distributed_cost - centralized_cost) / centralized_cost
    legacy_model_loss_mwh = float(
        sum(
            np.sum(np.asarray(legacy_centralized.microgrids[name]["loss_mw"]))
            * case.assumptions.dt_hours
            for name in names
        )
    )
    legacy_ac_loss_mwh = float(
        sum(float(item["total_loss_mwh"]) for item in legacy_ac_validation["microgrids"].values())
    )
    exact_model_loss_mwh = float(centralized.cluster["total_active_loss_mwh"])
    exact_ac_loss_mwh = float(
        sum(
            float(item["total_loss_mwh"])
            for item in centralized_checked.ac_validation["microgrids"].values()
        )
    )
    plan_times = tuple(float(t * 60) for t in case.time_hours)
    stage_evidence = (
        (
            "centralized_reference",
            {
                name: RegionalImport(
                    name,
                    plan_times,
                    tuple(float(v) for v in centralized.microgrids[name]["p_grid_mw"]),
                    (bool(centralized_checked.passed),) * len(plan_times),
                )
                for name in names
            },
        ),
        (
            "admm_reference",
            {
                name: RegionalImport(
                    name,
                    plan_times,
                    tuple(float(v) for v in admm.p_references_mw[name]),
                    (bool(admm.converged),) * len(plan_times),
                )
                for name in names
            },
        ),
        (
            "day_ahead",
            {
                name: RegionalImport(
                    name,
                    plan_times,
                    tuple(float(v) for v in local_results[name].microgrids[name]["p_grid_mw"]),
                    (bool(local_consistency[name].passed),) * len(plan_times),
                )
                for name in names
            },
        ),
        (
            "intraday",
            {
                name: RegionalImport(
                    name,
                    plan_times,
                    tuple(float(v) for v in intraday_results[name].microgrids[name]["p_grid_mw"]),
                    (bool(intraday_consistency[name].passed),) * len(plan_times),
                )
                for name in names
            },
        ),
        (
            "minute",
            {
                name: RegionalImport(
                    name,
                    tuple(float(t) for t in item.time_minutes),
                    tuple(float(v) for v in item.pcc_actual_mw),
                    (item.network_invalid_steps == 0,) * len(item.time_minutes),
                )
                for name, item in tracking.items()
            },
        ),
    )
    cluster_validation = summarize_cluster_validation(
        tuple(
            assess_cluster_import(
                stage=stage,
                expected_regions=names,
                trajectories=tuple(evidence.values()),
                limit_mw=case.cluster_import_limit_mw,
            )
            for stage, evidence in stage_evidence
        ),
        scope="hierarchical_execution",
    )
    cluster_limit_pass = cluster_validation.assessed_scope_status == "passed"
    reactive_execution_pass = all(
        bool(np.all(item.reactive_execution_known))
        and float(np.max(item.reactive_dispatch_unserved_mvar)) <= 1e-8
        for item in tracking.values()
    )
    regional_device_safety_pass = reactive_execution_pass and all(
        item.network_security_passed
        and item.no_reverse_violations_after_safety == 0
        and item.pf_violations_after_safety == 0
        for item in tracking.values()
    )
    minute_cluster_pass = cluster_validation.stages[-1].status == "passed"
    device_safety_pass = regional_device_safety_pass and minute_cluster_pass
    day_ahead_security_headroom = np.concatenate(
        [
            np.asarray(local_results[name].microgrids[name]["p_grid_security_headroom_mw"])
            for name in names
        ]
    )
    intraday_security_headroom = np.concatenate(
        [
            np.asarray(intraday_results[name].microgrids[name]["p_grid_security_headroom_mw"])
            for name in names
        ]
    )
    comparison: Dict[str, float | bool] = {
        "admm_converged": admm.converged,
        "centralized_ac_consistency_passed": centralized_checked.passed,
        "all_regional_ac_consistency_passed": all(
            item.passed for item in local_consistency.values()
        ),
        "all_intraday_ac_consistency_passed": all(
            item.passed for item in intraday_consistency.values()
        ),
        "centralized_economic_cost_cny": centralized_cost,
        "distributed_realization_cost_cny": distributed_cost,
        "distributed_cost_gap_percent": float(objective_gap),
        "intraday_updated_cost_cny": intraday_cost,
        "p_reference_realization_rmse_mw": float(np.sqrt(np.mean((realized_p - target_p) ** 2))),
        "q_reference_realization_rmse_mvar": float(np.sqrt(np.mean((realized_q - target_q) ** 2))),
        "cluster_import_limit_passed": cluster_limit_pass,
        "local_ac_validation_passed": _combined_ac_pass(case, local_results),
        "intraday_ac_validation_passed": _combined_ac_pass(intraday_case, intraday_results),
        "device_safety_passed": device_safety_pass,
        "storage_ordinary_ramp_passed": all(
            bool(item.storage_dynamics)
            and len(item.storage_dynamics) == len(item.time_minutes)
            and all(
                r.ramp_compliant or r.physical_override or r.hard_override
                for r in item.storage_dynamics
            )
            for item in tracking.values()
        ),
        "storage_physical_override_steps": sum(
            r.physical_override for item in tracking.values() for r in item.storage_dynamics
        ),
        "storage_hard_override_steps": sum(
            r.hard_override for item in tracking.values() for r in item.storage_dynamics
        ),
        "regional_device_safety_passed": regional_device_safety_pass,
        "minute_cluster_import_limit_passed": minute_cluster_pass,
        "day_ahead_cluster_import_limit_passed": cluster_validation.stages[2].status == "passed",
        "intraday_cluster_import_limit_passed": cluster_validation.stages[3].status == "passed",
        "minute_network_security_passed": all(
            item.network_security_passed for item in tracking.values()
        ),
        "minute_network_invalid_steps": sum(
            item.network_invalid_steps for item in tracking.values()
        ),
        "minute_network_violation_steps": sum(
            item.network_violation_steps for item in tracking.values()
        ),
        "reactive_execution_passed": reactive_execution_pass,
        "maximum_reactive_unserved_mvar": float(
            max(np.max(item.reactive_dispatch_unserved_mvar) for item in tracking.values())
        ),
        "day_ahead_planning_security_passed": bool(np.min(day_ahead_security_headroom) >= -1e-6),
        "intraday_planning_security_passed": bool(np.min(intraday_security_headroom) >= -1e-6),
        "minimum_day_ahead_security_headroom_mw": float(np.min(day_ahead_security_headroom)),
        "minimum_intraday_security_headroom_mw": float(np.min(intraday_security_headroom)),
        "mean_device_p_tracking_rmse_mw": float(
            np.mean([item.p_tracking_rmse_mw for item in tracking.values()])
        ),
        "mean_device_q_tracking_rmse_mvar": float(
            np.mean([item.q_tracking_rmse_mvar for item in tracking.values()])
        ),
        "group_control_curtailment_commands": float(
            sum(item.group_curtailment_commands for item in tracking.values())
        ),
        "group_control_restoration_commands": float(
            sum(item.group_restoration_commands for item in tracking.values())
        ),
        "group_control_recovery_aborts": float(
            sum(item.group_recovery_aborts for item in tracking.values())
        ),
        "local_reverse_flow_interventions": float(
            sum(item.local_reverse_flow_interventions for item in tracking.values())
        ),
        "legacy_model_active_loss_mwh": legacy_model_loss_mwh,
        "legacy_ac_active_loss_mwh": legacy_ac_loss_mwh,
        "legacy_loss_underestimate_percent": float(
            100.0 * (legacy_ac_loss_mwh - legacy_model_loss_mwh) / legacy_ac_loss_mwh
        ),
        "misocp_model_active_loss_mwh": exact_model_loss_mwh,
        "misocp_ac_active_loss_mwh": exact_ac_loss_mwh,
        "misocp_loss_relative_error_percent": float(
            100.0 * abs(exact_ac_loss_mwh - exact_model_loss_mwh) / exact_ac_loss_mwh
        ),
        "ac_loss_reduction_vs_legacy_dispatch_percent": float(
            100.0 * (legacy_ac_loss_mwh - exact_ac_loss_mwh) / legacy_ac_loss_mwh
        ),
    }
    comparison["overall_passed"] = bool(
        admm.converged
        and cluster_limit_pass
        and device_safety_pass
        and comparison["centralized_ac_consistency_passed"]
        and comparison["all_regional_ac_consistency_passed"]
        and comparison["all_intraday_ac_consistency_passed"]
        and comparison["day_ahead_planning_security_passed"]
        and comparison["intraday_planning_security_passed"]
    )
    return HierarchicalResult(
        admm=admm,
        centralized_reference=centralized,
        local_milp_results=local_results,
        intraday_milp_results=intraday_results,
        tracking=tracking,
        comparison=comparison,
        centralized_ac_consistency=centralized_checked,
        local_ac_consistency=local_consistency,
        intraday_ac_consistency=intraday_consistency,
        legacy_centralized_reference=legacy_centralized,
        day_ahead_security=day_ahead_security,
        intraday_security=intraday_security,
        cluster_validation=cluster_validation,
    )

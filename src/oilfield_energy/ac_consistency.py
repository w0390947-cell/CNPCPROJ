"""Fast cone/AC feedback and bounded exact-physics feasibility restoration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import acos, isfinite, tan
from time import monotonic
from typing import Dict, Mapping, Sequence, cast

import numpy as np

from .ac_power_flow import validate_ac_dispatch
from .data import ProjectCase
from .misocp_model import solve_case_misocp
from .model import OptimizationResult
from .planning_security import resolve_security_floors
from .modules.dispatch.api import solve_for_quality, run_ac_feasibility_recovery
from .modules.dispatch.contracts import (
    ACRecoveryAttempt,
    ACRecoveryEvidence,
    ACRecoveryPhase,
    ACRecoveryPolicy,
    ComputationQualityPolicy,
    JointNetworkTarget,
    OptimizationAttempt,
    OptimizationQuality,
    PCCTrackingLimits,
)


@dataclass(frozen=True)
class ACConsistencyConfig:
    max_feedback_iterations: int = 3
    maximum_cone_relative_gap: float = 1e-6
    maximum_pcc_p_difference_mw: float = 1e-4
    maximum_pcc_q_difference_mvar: float = 1e-4
    maximum_voltage_difference_pu: float = 1e-5
    maximum_line_loss_difference_mw: float = 1e-5
    loss_tightening_growth: float = 2.0
    maximum_voltage_security_margin_pu: float = 0.005


@dataclass
class ACConsistencyIteration:
    iteration: int
    solver_success: bool
    solver_status: str
    mip_gap: float | None
    cone_relative_gap: float
    pcc_p_difference_mw: float
    pcc_q_difference_mvar: float
    voltage_difference_pu: float
    line_p_loss_difference_mw: float
    line_q_loss_difference_mvar: float
    active_loss_difference_mwh: float
    reactive_loss_difference_mvarh: float
    extra_loss_tightening_cny_per_mwh: float
    voltage_security_margin_pu: float
    storage_binary_relaxation_used: bool
    storage_integrality_certified: bool
    passed: bool


@dataclass
class ACConsistencyResult:
    optimization: OptimizationResult
    ac_validation: Dict[str, object]
    passed: bool
    stop_reason: str
    iterations: int
    history: list[ACConsistencyIteration] = field(default_factory=list)
    optimization_quality: tuple[OptimizationQuality, ...] = ()
    recovery: ACRecoveryEvidence | None = None
    selected_consistency_iteration: int | None = None


def _maximum(report: Dict[str, object], key: str) -> float:
    microgrids = cast(dict[str, dict[str, float]], report.get("microgrids", {}))
    values = [float(item.get(key, 0.0)) for item in microgrids.values()]
    return max(values, default=float("inf"))


def _validate_iteration(
    case: ProjectCase,
    result: OptimizationResult,
    names: Sequence[str],
    config: ACConsistencyConfig,
    iteration: int,
    extra_loss_penalty: float,
    voltage_margin: float,
) -> tuple[Dict[str, object], ACConsistencyIteration]:
    validation = validate_ac_dispatch(case, result, names)
    cone_gap = float(
        cast(float, result.cluster.get("maximum_cone_relative_gap", float("inf")))
    )
    p_diff = _maximum(validation, "maximum_pcc_p_difference_from_linear_mw")
    q_diff = _maximum(validation, "maximum_pcc_q_difference_from_optimization_mvar")
    v_diff = _maximum(validation, "maximum_voltage_difference_from_optimization_pu")
    p_loss_diff = _maximum(validation, "maximum_line_p_loss_difference_mw")
    q_loss_diff = _maximum(validation, "maximum_line_q_loss_difference_mvar")
    ac_active_loss = float(
        sum(
            float(item["total_loss_mwh"])
            for item in cast(
                dict[str, dict[str, float]], validation["microgrids"]
            ).values()
        )
    )
    ac_reactive_loss = float(
        sum(
            float(item["total_reactive_loss_mvarh"])
            for item in cast(
                dict[str, dict[str, float]], validation["microgrids"]
            ).values()
        )
    )
    model_active_loss = float(
        cast(float, result.cluster.get("total_active_loss_mwh", float("nan")))
    )
    model_reactive_loss = float(
        cast(float, result.cluster.get("total_reactive_loss_mvarh", float("nan")))
    )
    active_loss_difference = abs(ac_active_loss - model_active_loss)
    reactive_loss_difference = abs(ac_reactive_loss - model_reactive_loss)
    passed = bool(
        validation.get("passed", False)
        and bool(result.cluster.get("storage_integrality_certified", False))
        and cone_gap <= config.maximum_cone_relative_gap
        and p_diff <= config.maximum_pcc_p_difference_mw
        and q_diff <= config.maximum_pcc_q_difference_mvar
        and v_diff <= config.maximum_voltage_difference_pu
        and p_loss_diff <= config.maximum_line_loss_difference_mw
        and q_loss_diff <= config.maximum_line_loss_difference_mw
    )
    record = ACConsistencyIteration(
        iteration=iteration,
        solver_success=True,
        solver_status=result.message,
        mip_gap=result.mip_gap,
        cone_relative_gap=cone_gap,
        pcc_p_difference_mw=p_diff,
        pcc_q_difference_mvar=q_diff,
        voltage_difference_pu=v_diff,
        line_p_loss_difference_mw=p_loss_diff,
        line_q_loss_difference_mvar=q_loss_diff,
        active_loss_difference_mwh=active_loss_difference,
        reactive_loss_difference_mvarh=reactive_loss_difference,
        extra_loss_tightening_cny_per_mwh=extra_loss_penalty,
        voltage_security_margin_pu=voltage_margin,
        storage_binary_relaxation_used=bool(
            result.cluster.get("storage_binary_relaxation_used", False)
        ),
        storage_integrality_certified=bool(
            result.cluster.get("storage_integrality_certified", False)
        ),
        passed=passed,
    )
    return validation, record


def solve_case_ac_consistent(
    case: ProjectCase,
    microgrid_names: Sequence[str] | None = None,
    *,
    storage_enabled: bool = True,
    cluster_coordination: bool = True,
    pcc_targets: Mapping[str, Mapping[str, np.ndarray]] | None = None,
    tracking_limits: PCCTrackingLimits | None = None,
    p_tracking_penalty_cny_per_mw: float = 20_000.0,
    q_tracking_penalty_cny_per_mvar: float = 8_000.0,
    time_limit_seconds: float = 300.0,
    relative_gap: float = 1e-4,
    relax_and_certify_storage_binaries: bool = True,
    consistency_config: ACConsistencyConfig | None = None,
    quality_policy: ComputationQualityPolicy | None = None,
    p_grid_security_floors_mw: Mapping[str, np.ndarray] | None = None,
    joint_network_target: JointNetworkTarget | None = None,
    recovery_policy: ACRecoveryPolicy | None = ACRecoveryPolicy(),
) -> ACConsistencyResult:
    """求解MISOCP，并仅在锥与独立AC结果一致时接受。

    正常径向算例中，正网损成本会使Branch Flow锥约束在第一次求解即取紧。
    若未取紧或AC误差超限，下一轮提高电流平方损耗惩罚，并按观测到的
    电压模型误差收紧电压边界。所有反馈量均记录，不能静默接受松弛解。
    """
    config = consistency_config or ACConsistencyConfig()
    if config.max_feedback_iterations < 1:
        raise ValueError("max_feedback_iterations must be positive")
    names = (
        list(microgrid_names)
        if microgrid_names is not None
        else [mg.name for mg in case.microgrids]
    )
    history: list[ACConsistencyIteration] = []
    extra_loss_penalty = 0.0
    voltage_margin = 0.0
    last_result: OptimizationResult | None = None
    last_validation: Dict[str, object] = {"passed": False, "microgrids": {}}
    quality_records: list[OptimizationQuality] = []
    deadline = (
        monotonic() + time_limit_seconds if joint_network_target is not None else None
    )
    fast_deadline = (
        monotonic() + time_limit_seconds * 0.5
        if deadline is not None and recovery_policy is not None
        else deadline
    )

    def solve_model(relax: bool) -> OptimizationResult:
        def attempt(seconds: float) -> tuple[OptimizationResult, OptimizationAttempt]:
            requested_seconds = seconds
            if fast_deadline is not None:
                seconds = max(0.001, min(seconds, fast_deadline - monotonic()))
            try:
                result = solve_case_misocp(
                    case,
                    names,
                    storage_enabled=storage_enabled,
                    cluster_coordination=cluster_coordination,
                    pcc_targets=pcc_targets,
                    tracking_limits=tracking_limits,
                    p_tracking_penalty_cny_per_mw=p_tracking_penalty_cny_per_mw,
                    q_tracking_penalty_cny_per_mvar=q_tracking_penalty_cny_per_mvar,
                    time_limit_seconds=seconds,
                    relative_gap=quality_policy.relative_gap
                    if quality_policy
                    else relative_gap,
                    relax_storage_binaries=relax,
                    extra_loss_tightening_cny_per_mwh=extra_loss_penalty,
                    voltage_security_margin_pu=voltage_margin,
                    p_grid_security_floors_mw=p_grid_security_floors_mw,
                    joint_network_target=joint_network_target,
                )
            except (RuntimeError, FloatingPointError) as exc:
                result = OptimizationResult(
                    success=False,
                    status=2,
                    message=f"solver_error: {exc}",
                    objective_cny=float("nan"),
                    solver_objective_without_constants_cny=float("nan"),
                    mip_gap=None,
                    microgrids={},
                    cluster={},
                    model_size={},
                )
            gap = result.mip_gap
            return result, OptimizationAttempt(
                budget_seconds=requested_seconds,
                solver_status=result.message.removeprefix("SCIP status: "),
                feasible=result.success,
                objective_cny=result.objective_cny
                if result.success and isfinite(result.objective_cny)
                else None,
                relative_gap=gap
                if gap is not None and isfinite(gap) and gap >= 0
                else None,
            )

        if quality_policy is None:
            return attempt(time_limit_seconds)[0]
        result, evidence = solve_for_quality(attempt, quality_policy)
        quality_records.append(evidence)
        return result

    for iteration in range(1, config.max_feedback_iterations + 1):
        if (
            fast_deadline is not None
            and monotonic() >= fast_deadline
            and last_result is not None
        ):
            break
        result = solve_model(relax_and_certify_storage_binaries)
        last_result = result
        if (
            result.success
            and relax_and_certify_storage_binaries
            and not bool(result.cluster.get("storage_integrality_certified", False))
        ):
            # 连续松弛出现实质性同时充放电，不能作为MISOCP整数最优证书；
            # 立即回退到原始二进制模型。
            result = solve_model(False)
            last_result = result
        if not result.success:
            history.append(
                ACConsistencyIteration(
                    iteration=iteration,
                    solver_success=False,
                    solver_status=result.message,
                    mip_gap=result.mip_gap,
                    cone_relative_gap=float("inf"),
                    pcc_p_difference_mw=float("inf"),
                    pcc_q_difference_mvar=float("inf"),
                    voltage_difference_pu=float("inf"),
                    line_p_loss_difference_mw=float("inf"),
                    line_q_loss_difference_mvar=float("inf"),
                    active_loss_difference_mwh=float("inf"),
                    reactive_loss_difference_mvarh=float("inf"),
                    extra_loss_tightening_cny_per_mwh=extra_loss_penalty,
                    voltage_security_margin_pu=voltage_margin,
                    storage_binary_relaxation_used=bool(
                        result.cluster.get("storage_binary_relaxation_used", False)
                    )
                    if result.cluster
                    else False,
                    storage_integrality_certified=bool(
                        result.cluster.get("storage_integrality_certified", False)
                    )
                    if result.cluster
                    else False,
                    passed=False,
                )
            )
            break

        validation, record = _validate_iteration(
            case, result, names, config, iteration, extra_loss_penalty, voltage_margin
        )
        last_validation = validation
        history.append(record)
        passed, v_diff = record.passed, record.voltage_difference_pu
        if passed:
            return ACConsistencyResult(
                optimization=result,
                ac_validation=validation,
                passed=True,
                stop_reason="misocp_cone_and_ac_tolerances_met",
                iterations=iteration,
                history=history,
                optimization_quality=tuple(quality_records),
            )

        # AC结果反馈：强化真实电流损耗的单调性，并根据观测误差增加电压裕度。
        if extra_loss_penalty == 0.0:
            extra_loss_penalty = case.assumptions.loss_value_cny_per_mwh
        else:
            extra_loss_penalty *= config.loss_tightening_growth
        voltage_margin = min(
            config.maximum_voltage_security_margin_pu,
            max(voltage_margin, 1.25 * v_diff),
        )

    if last_result is None:
        raise RuntimeError("AC consistency loop did not execute")
    failed = ACConsistencyResult(
        optimization=last_result,
        ac_validation=last_validation,
        passed=False,
        stop_reason=(
            {
                "SCIP status: infeasible": "solver_infeasible",
                "SCIP status: timelimit": "solver_time_limit_without_plan",
            }.get(last_result.message, "solver_failed")
            if not last_result.success
            else "ac_consistency_iterations_exhausted"
        ),
        iterations=len(history),
        history=history,
        optimization_quality=tuple(quality_records),
    )
    # A tightened auxiliary model is not the original feasibility problem.
    if (
        failed.stop_reason == "solver_infeasible"
        and history[-1].voltage_security_margin_pu > 0
    ):
        failed.stop_reason = "restricted_model_infeasible"
    if recovery_policy is None or failed.stop_reason == "solver_infeasible":
        return failed

    budget = min(recovery_policy.maximum_seconds, time_limit_seconds)
    fast_feedback = tuple(
        {
            key: None if isinstance(value, float) and not isfinite(value) else value
            for key, value in asdict(row).items()
        }
        for row in history
    )
    if deadline is not None:
        budget = min(budget, deadline - monotonic())
    if budget <= 0:
        return failed

    last_restoration: OptimizationResult | None = None

    def restore(phase: ACRecoveryPhase, seconds: float):
        nonlocal last_restoration
        started = monotonic()
        objective = (
            "constant_feasibility"
            if phase == "physical_feasibility"
            else (
                "joint_lexicographic"
                if joint_network_target is not None
                else "original_economic"
            )
        )
        try:
            restored = solve_case_misocp(
                case,
                names,
                storage_enabled=storage_enabled,
                cluster_coordination=cluster_coordination,
                pcc_targets=pcc_targets,
                tracking_limits=tracking_limits,
                p_tracking_penalty_cny_per_mw=p_tracking_penalty_cny_per_mw,
                q_tracking_penalty_cny_per_mvar=q_tracking_penalty_cny_per_mvar,
                time_limit_seconds=seconds,
                relative_gap=quality_policy.relative_gap
                if quality_policy
                else relative_gap,
                relax_storage_binaries=False,
                extra_loss_tightening_cny_per_mwh=0.0,
                voltage_security_margin_pu=0.0,
                p_grid_security_floors_mw=p_grid_security_floors_mw,
                joint_network_target=joint_network_target,
                exact_ac_physics=True,
                objective_mode="feasibility"
                if phase == "physical_feasibility"
                else "original",
            )
            last_restoration = restored
        except (RuntimeError, FloatingPointError) as exc:
            return None, ACRecoveryAttempt(
                phase=phase,
                budget_seconds=seconds,
                elapsed_seconds=monotonic() - started,
                objective=objective,
                solver_status="error",
                solver_feasible=False,
                certified=False,
                reason=str(exc),
                target_relative_gap=quality_policy.relative_gap
                if quality_policy
                else relative_gap,
                p_tracking_penalty_cny_per_mw=p_tracking_penalty_cny_per_mw,
                q_tracking_penalty_cny_per_mvar=q_tracking_penalty_cny_per_mvar,
            )
        checks: dict[str, bool] = {}
        metrics: dict[str, float | None] = {}
        record: ACConsistencyIteration | None = None
        report: Dict[str, object] = {"passed": False, "microgrids": {}}
        if restored.success:
            try:
                report, record = _validate_iteration(
                    case, restored, names, config, len(history) + 1, 0.0, 0.0
                )
                history.append(record)
                checks = {
                    "ac_consistency": record.passed,
                    "original_model_constraints": restored.cluster.get(
                        "original_constraints_verified"
                    )
                    is True,
                    **_physical_adoption_checks(
                        case,
                        names,
                        report,
                        pcc_targets,
                        tracking_limits,
                        cluster_coordination,
                        joint_network_target,
                        p_grid_security_floors_mw,
                    ),
                }
                metrics = {
                    "cone_relative_gap": record.cone_relative_gap,
                    "pcc_p_difference_mw": record.pcc_p_difference_mw,
                    "pcc_q_difference_mvar": record.pcc_q_difference_mvar,
                    "voltage_difference_pu": record.voltage_difference_pu,
                    "line_p_loss_difference_mw": record.line_p_loss_difference_mw,
                    "line_q_loss_difference_mvar": record.line_q_loss_difference_mvar,
                }
            except (ValueError, RuntimeError, FloatingPointError) as exc:
                return None, ACRecoveryAttempt(
                    phase=phase,
                    budget_seconds=seconds,
                    elapsed_seconds=monotonic() - started,
                    objective=objective,
                    solver_status=restored.message.removeprefix("SCIP status: "),
                    solver_feasible=True,
                    certified=False,
                    reason=f"certification failed: {exc}",
                    target_relative_gap=quality_policy.relative_gap
                    if quality_policy
                    else relative_gap,
                    p_tracking_penalty_cny_per_mw=p_tracking_penalty_cny_per_mw,
                    q_tracking_penalty_cny_per_mvar=q_tracking_penalty_cny_per_mvar,
                )
        metrics = {
            key: value if value is not None and isfinite(value) else None
            for key, value in metrics.items()
        }
        certified = bool(checks) and all(checks.values())
        gap = restored.mip_gap
        gap = gap if gap is not None and isfinite(gap) and gap >= 0 else None
        joint = cast(
            dict[str, object], restored.cluster.get("joint_network_recovery", {})
        )
        original_proven = bool(
            certified
            and phase == "original_objective"
            and restored.cluster.get("solved_objective") == objective
            and gap is not None
            and gap <= (quality_policy.relative_gap if quality_policy else relative_gap)
            and (joint_network_target is None or joint.get("deviation_optimal") is True)
        )
        row = ACRecoveryAttempt(
            phase=phase,
            budget_seconds=seconds,
            elapsed_seconds=monotonic() - started,
            objective=objective,
            solver_status=restored.message.removeprefix("SCIP status: "),
            solver_feasible=restored.success,
            certified=certified,
            checks=checks,
            metrics=metrics,
            original_objective_cny=cast(
                float | None, restored.cluster.get("original_objective_cny")
            ),
            solver_objective_value=cast(
                float | None, restored.cluster.get("solver_objective_value")
            ),
            relative_gap=gap,
            reference_distance=cast(float | None, joint.get("deviation")),
            target_relative_gap=quality_policy.relative_gap
            if quality_policy
            else relative_gap,
            original_optimality_proven=original_proven,
            p_tracking_penalty_cny_per_mw=p_tracking_penalty_cny_per_mw,
            q_tracking_penalty_cny_per_mvar=q_tracking_penalty_cny_per_mvar,
            infeasibility_scope="original_model"
            if restored.message == "SCIP status: infeasible"
            and phase == "physical_feasibility"
            else "restricted_model"
            if restored.message == "SCIP status: infeasible"
            else "none",
            reason="" if certified else "未取得通过原约束及独立 AC 校核的恢复计划",
        )
        candidate = None
        if certified:
            assert record is not None
            candidate = restored, report, record.iteration
        return candidate, row

    selection = run_ac_feasibility_recovery(
        policy=recovery_policy,
        trigger=failed.stop_reason,
        budget_seconds=budget,
        solve=restore,
        clock=monotonic,
    )
    evidence = selection.evidence.model_copy(update={"fast_feedback": fast_feedback})
    failed.recovery = evidence
    failed.iterations = len(history)
    if selection.candidate is None:
        if evidence.outcome == "model_infeasible" and last_restoration is not None:
            failed.optimization = last_restoration
        failed.stop_reason = (
            "solver_infeasible"
            if selection.evidence.outcome == "model_infeasible"
            else "ac_recovery_unresolved"
        )
        return failed
    selected, validation, selected_iteration = selection.candidate
    return ACConsistencyResult(
        optimization=selected,
        ac_validation=validation,
        passed=True,
        stop_reason="ac_physical_recovery_certified",
        iterations=len(history),
        history=history,
        optimization_quality=tuple(quality_records),
        recovery=evidence,
        selected_consistency_iteration=selected_iteration,
    )


def _physical_adoption_checks(
    case: ProjectCase,
    names: Sequence[str],
    report: Dict[str, object],
    targets: Mapping[str, Mapping[str, np.ndarray]] | None,
    tracking: PCCTrackingLimits | None,
    cluster_coordination: bool,
    joint: JointNetworkTarget | None,
    security_floors: Mapping[str, np.ndarray] | None,
) -> dict[str, bool]:
    """Check actual independent AC powers, not just optimizer target slacks."""
    checks = {}
    grids = {mg.name: mg for mg in case.microgrids}
    floors = resolve_security_floors(case, [grids[n] for n in names], security_floors)
    p_values, q_values = [], []
    tol = tracking.numerical_tolerance if tracking is not None else 1e-6
    for name in names:
        local = cast(dict[str, dict[str, list[float]]], report["microgrids"])[name]
        p, q = np.array(local["pcc_p_mw"]), np.array(local["pcc_q_mvar"])
        p_values.append(p)
        q_values.append(q)
        checks[f"{name}:pcc_bounds"] = bool(
            np.all(p <= grids[name].p_grid_max_mw + tol)
            and np.all(p >= grids[name].p_grid_min_mw - tol)
        )
        checks[f"{name}:planning_pf"] = bool(
            np.all(
                abs(q)
                <= tan(acos(case.assumptions.pf_dispatch_target)) * (p - floors[name])
                + tol
            )
        )
        checks[f"{name}:q_bounds"] = bool(
            np.all(abs(q) <= grids[name].p_grid_max_mw + tol)
        )
        if tracking is not None and targets is not None:
            checks[f"{name}:p_tracking"] = bool(
                np.all(abs(p - targets[name]["p_mw"]) <= tracking.p_mw + tol)
            )
            checks[f"{name}:q_tracking"] = bool(
                np.all(abs(q - targets[name]["q_mvar"]) <= tracking.q_mvar + tol)
            )
    if cluster_coordination and len(names) > 1:
        total_p, total_q = np.sum(p_values, axis=0), np.sum(q_values, axis=0)
        checks["cluster_import"] = bool(
            np.all(total_p <= case.cluster_import_limit_mw + tol)
        )
        if joint is not None:
            checks["cluster_power_factor"] = bool(
                np.all(
                    abs(total_q)
                    <= tan(acos(joint.pf_target)) * total_p
                    - np.array(joint.pf_margin_mvar)
                    + tol
                )
            )
    return checks

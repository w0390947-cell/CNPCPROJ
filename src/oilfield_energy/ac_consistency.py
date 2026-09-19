"""MISOCP锥精确性与独立AC潮流一致性闭环。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Sequence

import numpy as np

from .ac_power_flow import validate_ac_dispatch
from .data import ProjectCase
from .misocp_model import solve_case_misocp
from .model import OptimizationResult


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


def _maximum(report: Dict[str, object], key: str) -> float:
    microgrids = report.get("microgrids", {})
    values = [float(item.get(key, 0.0)) for item in microgrids.values()]
    return max(values, default=float("inf"))


def solve_case_ac_consistent(
    case: ProjectCase,
    microgrid_names: Sequence[str] | None = None,
    *,
    storage_enabled: bool = True,
    cluster_coordination: bool = True,
    pcc_targets: Mapping[str, Mapping[str, np.ndarray]] | None = None,
    p_tracking_penalty_cny_per_mw: float = 20_000.0,
    q_tracking_penalty_cny_per_mvar: float = 8_000.0,
    time_limit_seconds: float = 300.0,
    relative_gap: float = 1e-4,
    relax_and_certify_storage_binaries: bool = True,
    consistency_config: ACConsistencyConfig | None = None,
    p_grid_security_floors_mw: Mapping[str, np.ndarray] | None = None,
) -> ACConsistencyResult:
    """求解MISOCP，并仅在锥与独立AC结果一致时接受。

    正常径向算例中，正网损成本会使Branch Flow锥约束在第一次求解即取紧。
    若未取紧或AC误差超限，下一轮提高电流平方损耗惩罚，并按观测到的
    电压模型误差收紧电压边界。所有反馈量均记录，不能静默接受松弛解。
    """
    config = consistency_config or ACConsistencyConfig()
    if config.max_feedback_iterations < 1:
        raise ValueError("max_feedback_iterations must be positive")
    names = list(microgrid_names) if microgrid_names is not None else [mg.name for mg in case.microgrids]
    history: list[ACConsistencyIteration] = []
    extra_loss_penalty = 0.0
    voltage_margin = 0.0
    last_result: OptimizationResult | None = None
    last_validation: Dict[str, object] = {"passed": False, "microgrids": {}}

    for iteration in range(1, config.max_feedback_iterations + 1):
        result = solve_case_misocp(
            case,
            names,
            storage_enabled=storage_enabled,
            cluster_coordination=cluster_coordination,
            pcc_targets=pcc_targets,
            p_tracking_penalty_cny_per_mw=p_tracking_penalty_cny_per_mw,
            q_tracking_penalty_cny_per_mvar=q_tracking_penalty_cny_per_mvar,
            time_limit_seconds=time_limit_seconds,
            relative_gap=relative_gap,
            relax_storage_binaries=relax_and_certify_storage_binaries,
            extra_loss_tightening_cny_per_mwh=extra_loss_penalty,
            voltage_security_margin_pu=voltage_margin,
            p_grid_security_floors_mw=p_grid_security_floors_mw,
        )
        last_result = result
        if (
            result.success
            and relax_and_certify_storage_binaries
            and not bool(result.cluster.get("storage_integrality_certified", False))
        ):
            # 连续松弛出现实质性同时充放电，不能作为MISOCP整数最优证书；
            # 立即回退到原始二进制模型。
            result = solve_case_misocp(
                case,
                names,
                storage_enabled=storage_enabled,
                cluster_coordination=cluster_coordination,
                pcc_targets=pcc_targets,
                p_tracking_penalty_cny_per_mw=p_tracking_penalty_cny_per_mw,
                q_tracking_penalty_cny_per_mvar=q_tracking_penalty_cny_per_mvar,
                time_limit_seconds=time_limit_seconds,
                relative_gap=relative_gap,
                relax_storage_binaries=False,
                extra_loss_tightening_cny_per_mwh=extra_loss_penalty,
                voltage_security_margin_pu=voltage_margin,
                p_grid_security_floors_mw=p_grid_security_floors_mw,
            )
            last_result = result
        if not result.success:
            history.append(ACConsistencyIteration(
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
                ) if result.cluster else False,
                storage_integrality_certified=bool(
                    result.cluster.get("storage_integrality_certified", False)
                ) if result.cluster else False,
                passed=False,
            ))
            break

        validation = validate_ac_dispatch(case, result, names)
        last_validation = validation
        cone_gap = float(result.cluster.get("maximum_cone_relative_gap", float("inf")))
        p_diff = _maximum(validation, "maximum_pcc_p_difference_from_linear_mw")
        q_diff = _maximum(validation, "maximum_pcc_q_difference_from_optimization_mvar")
        v_diff = _maximum(validation, "maximum_voltage_difference_from_optimization_pu")
        p_loss_diff = _maximum(validation, "maximum_line_p_loss_difference_mw")
        q_loss_diff = _maximum(validation, "maximum_line_q_loss_difference_mvar")
        ac_active_loss = float(sum(
            float(item["total_loss_mwh"])
            for item in validation["microgrids"].values()
        ))
        ac_reactive_loss = float(sum(
            float(item["total_reactive_loss_mvarh"])
            for item in validation["microgrids"].values()
        ))
        model_active_loss = float(result.cluster.get("total_active_loss_mwh", float("nan")))
        model_reactive_loss = float(result.cluster.get("total_reactive_loss_mvarh", float("nan")))
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
        history.append(ACConsistencyIteration(
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
        ))
        if passed:
            return ACConsistencyResult(
                optimization=result,
                ac_validation=validation,
                passed=True,
                stop_reason="misocp_cone_and_ac_tolerances_met",
                iterations=iteration,
                history=history,
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
    return ACConsistencyResult(
        optimization=last_result,
        ac_validation=last_validation,
        passed=False,
        stop_reason="solver_failed" if not last_result.success else "ac_consistency_iterations_exhausted",
        iterations=len(history),
        history=history,
    )

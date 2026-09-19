"""网页与其他交互式客户端共用的纯仿真服务入口。

本模块不依赖 HTTP，也不写出图片。调用方提交经过校验的请求后，可获得
稳定、带单位、可直接 JSON 序列化的结果契约。现有数学模型仍由原模块实现。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ac_consistency import solve_case_ac_consistent
from .ac_power_flow import validate_ac_dispatch
from .admm import run_admm_coordination
from .analysis import compare_results, validate_result
from .data import MicrogridData, ProjectCase, build_synthetic_case
from .group_control_scenarios import run_group_control_scenarios
from .hierarchy_types import ADMMConfig, CommunicationConfig
from .model import OptimizationResult, solve_case
from .modules.dispatch.api import assess_reference_economics
from .modules.dispatch.contracts import (
    ACCOUNTING_VERSION,
    CoordinationSnapshot,
    DispatchCapabilities,
    ReferenceEconomics,
)
from .modules.power_flow.api import assess_cluster_import, summarize_cluster_validation
from .modules.power_flow.contracts import ClusterValidation, RegionalImport
from .modules.studies.api import compile_communication_events, validate_scenario_events
from .modules.studies.contracts import CommunicationEventExecution, ScenarioEventRecord
from .network_model import validate_legacy_network_alignment
from .scenario_events import EventType, ScenarioEvent, apply_physical_events

SCHEMA_VERSION = "1.3.0"
SIMULATED_DATA_NOTICE = "全部网络与运行数据均为参数化模拟数据，不代表实际油田数据。"


class ScenarioType(str, Enum):
    SINGLE_MICROGRID = "single_microgrid"
    CLUSTER_COORDINATION = "cluster_coordination"
    COMMUNICATION_FAULT = "communication_fault"
    GROUP_CONTROL = "group_control"


class SolverFormulation(str, Enum):
    MISOCP = "misocp"
    LEGACY_MILP = "legacy_milp"


class SimulationStage(str, Enum):
    PREPARING_CASE = "preparing_case"
    SOLVING_BASELINE = "solving_baseline"
    SOLVING_OPTIMIZED = "solving_optimized"
    VALIDATING = "validating"
    SERIALIZING = "serializing"
    SUCCEEDED = "succeeded"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SolverOptions(ContractModel):
    formulation: SolverFormulation = SolverFormulation.MISOCP
    time_limit_seconds: float = Field(default=60.0, gt=0.0, le=1800.0)


class SimulationRequestRecord(ContractModel):
    """Recorded inputs, including historical combinations no longer executable."""

    name: str = Field(default="单微网快速仿真", min_length=1, max_length=80)
    scenario_type: ScenarioType = ScenarioType.SINGLE_MICROGRID
    region: Literal["SC", "YA_B", "YA_C"] = "SC"
    steps: int = Field(default=8, ge=1, le=288)
    storage_enabled: bool = True
    solver: SolverOptions = Field(default_factory=SolverOptions)
    admm_max_iterations: int = Field(default=220, ge=4, le=1000)
    communication_loss_probability: float = Field(default=0.05, ge=0.0, le=1.0)
    communication_max_delay_iterations: int = Field(default=2, ge=0, le=20)
    communication_outage_region: Literal["SC", "YA_B", "YA_C"] = "YA_B"
    communication_outage_start_iteration: int = Field(default=12, ge=1, le=1000)
    communication_outage_end_iteration: int = Field(default=22, ge=1, le=1000)
    events: list[ScenarioEventRecord] = Field(default_factory=list, max_length=20)


class SimulationRequest(SimulationRequestRecord):
    """Executable request; validated separately from stored historical evidence."""

    events: list[ScenarioEvent] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_scenario_options(self) -> "SimulationRequest":
        validate_scenario_events(
            self.scenario_type.value, self.region, self.events, self.admm_max_iterations
        )
        if self.communication_outage_end_iteration < self.communication_outage_start_iteration:
            raise ValueError("communication outage end must not precede its start")
        if (
            self.scenario_type in {
                ScenarioType.CLUSTER_COORDINATION,
                ScenarioType.COMMUNICATION_FAULT,
            }
            and self.steps < 4
        ):
            raise ValueError("cluster scenarios require at least four time steps")
        return self


class ProgressUpdate(ContractModel):
    stage: SimulationStage
    label: str
    sequence: int = Field(ge=1)
    total_stages: int = Field(default=6, ge=1)


class TopologyNode(ContractModel):
    id: str
    label: str
    kind: Literal["pcc", "main", "wind", "pv", "flex"]


class TopologyEdge(ContractModel):
    id: str
    source: str
    target: str
    resistance_pu: float
    reactance_pu: float
    capacity_mva: float
    branch_kind: Literal["line", "transformer"] = "line"
    tap_ratio: float = 1.0


class ValidationItem(ContractModel):
    code: str
    label: str
    passed: bool
    actual: bool | int | float | str | None = None
    limit: bool | int | float | str | None = None
    unit: str | None = None
    scope: str
    explanation: str
    validation_basis: Literal["centralized_reference", "admm_reference"] | None = None


class TimeSeriesPoint(ContractModel):
    time_hour: float
    price_cny_per_mwh: float
    load_mw: float
    load_mvar: float
    p_grid_baseline_mw: float
    p_grid_optimized_mw: float
    q_grid_optimized_mvar: float
    power_factor: float
    wind_available_mw: float
    wind_used_mw: float
    pv_available_mw: float
    pv_used_mw: float
    storage_charge_mw: float
    storage_discharge_mw: float
    storage_energy_mwh: float
    svg_q_mvar: float
    minimum_voltage_pu: float
    maximum_voltage_pu: float
    maximum_line_loading_pu: float
    active_loss_mw: float
    p_grid_security_floor_mw: float
    p_grid_security_headroom_mw: float


class ClusterTimeSeriesPoint(ContractModel):
    time_hour: float
    aggregate_load_mw: float
    aggregate_renewable_mw: float
    aggregate_import_mw: float
    aggregate_q_mvar: float
    import_limit_mw: float
    regional_import_mw: dict[str, float]


class ADMMHistoryPoint(ContractModel):
    iteration: int
    primal_residual: float
    dual_residual: float
    primal_tolerance: float
    dual_tolerance: float
    fresh_region_count: int
    convergence_streak: int
    fallback_regions: list[str]


class CommunicationSummary(ContractModel):
    sent: int
    delivered: int
    dropped: int
    delayed: int
    outage_dropped: int
    stale_uses: int
    fallback_uses: int
    loss_probability: float
    loss_start_iteration: int | None = None
    loss_end_iteration: int | None = None
    outage_region: str | None = None
    outage_start_iteration: int | None = None
    outage_end_iteration: int | None = None
    event_executions: tuple[CommunicationEventExecution, ...] = ()


class GroupControlRecord(ContractModel):
    scenario: str
    time_minute: float
    pcc_power_mw: float
    risk_limit_mw: float
    safety_threshold_mw: float
    restore_threshold_mw: float
    risk_index: float
    state: str
    action: str
    trigger_reason: str
    requested_curtailment_mw: float
    requested_restoration_mw: float
    remaining_curtailment_mw: float


class GroupControlEventRecord(ContractModel):
    scenario: str
    time_minute: float
    event_type: str
    reason: str
    previous_state: str
    new_state: str
    requested_power_mw: float
    achieved_power_mw: float


class GroupControlSummary(ContractModel):
    all_passed: bool
    scenario_count: int
    scenario_checks: dict[str, dict[str, bool]]
    records: list[GroupControlRecord]
    events: list[GroupControlEventRecord]


class SimulationMetadata(ContractModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: str
    data_source: Literal["parameterized_simulation"] = "parameterized_simulation"
    data_notice: str = SIMULATED_DATA_NOTICE
    scenario_type: ScenarioType
    region: str
    steps: int
    dt_hours: float
    formulation: SolverFormulation


class ExecutiveSummary(ContractModel):
    overall_passed: bool
    headline: str
    economic_improvement_percent: float
    baseline_economic_cost_cny: float
    optimized_economic_cost_cny: float
    optimized_peak_import_mw: float
    total_active_loss_mwh: float
    minimum_power_factor: float
    minimum_voltage_pu: float
    maximum_voltage_pu: float


class SimulationResult(ContractModel):
    metadata: SimulationMetadata
    request: SimulationRequestRecord
    executive_summary: ExecutiveSummary
    topology_nodes: list[TopologyNode]
    topology_edges: list[TopologyEdge]
    validation_items: list[ValidationItem]
    comparison: dict[str, float]
    solver: dict[str, Any]
    timeseries: list[TimeSeriesPoint]
    cluster_timeseries: list[ClusterTimeSeriesPoint] = Field(default_factory=list)
    admm_history: list[ADMMHistoryPoint] = Field(default_factory=list)
    communication: CommunicationSummary | None = None
    group_control: GroupControlSummary | None = None
    # Missing on historical v1.0 results: never relabel old accounting as v2.
    economic_accounting_version: str | None = None
    reference_economics: ReferenceEconomics | None = None
    cluster_validation: ClusterValidation | None = None
    coordination_snapshot: CoordinationSnapshot | None = None


ProgressCallback = Callable[[ProgressUpdate], None]


def _emit(callback: ProgressCallback | None, stage: SimulationStage, label: str, sequence: int) -> None:
    if callback is not None:
        callback(ProgressUpdate(stage=stage, label=label, sequence=sequence))


def _solve(
    case: ProjectCase,
    names: list[str],
    *,
    formulation: SolverFormulation,
    storage_enabled: bool,
    time_limit_seconds: float,
    cluster_coordination: bool = False,
) -> tuple[OptimizationResult, dict[str, Any]]:
    if formulation is SolverFormulation.LEGACY_MILP:
        result = solve_case(
            case,
            names,
            storage_enabled=storage_enabled,
            cluster_coordination=cluster_coordination,
            time_limit_seconds=time_limit_seconds,
        )
        return result, {
            "ac_consistency_required": False,
            "security_floor_basis": "legacy_pcc_import_lower_bound",
            "reactive_loss_basis": "legacy_lindistflow_omits_series_reactive_losses",
        }

    checked = solve_case_ac_consistent(
        case,
        names,
        storage_enabled=storage_enabled,
        cluster_coordination=cluster_coordination,
        time_limit_seconds=time_limit_seconds,
    )
    if not checked.passed:
        raise RuntimeError(f"AC consistency failed: {checked.stop_reason}")
    last = checked.history[-1]
    return checked.optimization, {
        "ac_consistency_required": True,
        "ac_consistency_passed": checked.passed,
        "ac_consistency_stop_reason": checked.stop_reason,
        "ac_consistency_iterations": checked.iterations,
        "maximum_cone_relative_gap": float(last.cone_relative_gap),
        "maximum_line_p_loss_difference_mw": float(last.line_p_loss_difference_mw),
        "maximum_line_q_loss_difference_mvar": float(last.line_q_loss_difference_mvar),
        "maximum_voltage_difference_pu": float(last.voltage_difference_pu),
    }


def _require_success(label: str, result: OptimizationResult) -> None:
    if not result.success:
        raise RuntimeError(
            f"{label} failed: status={result.status}, message={result.message}"
        )


def _node_kind(bus: str) -> Literal["pcc", "main", "wind", "pv", "flex"]:
    suffix = bus.rsplit("_", 1)[-1].lower()
    if suffix in {"pcc", "main", "wind", "pv", "flex"}:
        return suffix  # type: ignore[return-value]
    raise ValueError(f"unsupported synthetic bus kind: {bus}")


def _topology(microgrid: MicrogridData) -> tuple[list[TopologyNode], list[TopologyEdge]]:
    lines = validate_legacy_network_alignment(microgrid).branches
    nodes = [
        TopologyNode(id=bus, label=bus.rsplit("_", 1)[-1], kind=_node_kind(bus))
        for bus in microgrid.buses
    ]
    edges = [
        TopologyEdge(
            id=line.name,
            source=line.parent,
            target=line.child,
            resistance_pu=line.r_pu,
            reactance_pu=line.x_pu,
            capacity_mva=line.s_max_mva,
            branch_kind=line.kind.value,
            tap_ratio=line.tap_ratio,
        )
        for line in lines
    ]
    return nodes, edges


def _validation_items(
    region: str,
    validation: dict[str, Any],
    ac_validation: dict[str, Any],
) -> list[ValidationItem]:
    local = validation["microgrids"][region]
    ac_local = ac_validation["microgrids"][region]
    specifications = [
        ("NO_REVERSE_FLOW", "PCC 防倒送", "no_reverse", "PCC 受电功率满足防倒送约束"),
        ("PLANNING_SECURITY", "计划安全下界", "planning_security_floor_compliant", "PCC 计划满足时变安全下界"),
        ("POWER_FACTOR", "功率因数", "pf_compliant", "功率因数不低于项目下限"),
        ("VOLTAGE", "节点电压", "voltage_compliant", "所有节点电压处于允许范围"),
        ("LINE_CAPACITY", "线路容量", "line_capacity_compliant", "所有线路负载率不超过容量"),
        ("STORAGE_MODE", "储能充放电互斥", "storage_not_simultaneous", "储能未同时充电和放电"),
        ("STORAGE_ENERGY", "储能能量边界", "storage_energy_compliant", "储能能量处于允许范围"),
    ]
    items = [
        ValidationItem(
            code=code,
            label=label,
            passed=bool(local[key]),
            actual=bool(local[key]),
            limit=True,
            scope=region,
            explanation=explanation,
        )
        for code, label, key, explanation in specifications
    ]
    items.append(ValidationItem(
        code="INDEPENDENT_AC",
        label="独立 AC 潮流复核",
        passed=bool(ac_local["passed"]),
        actual=bool(ac_local["passed"]),
        limit=True,
        scope=region,
        explanation="独立 AC 潮流收敛且电压、线路、功率因数和倒送复核通过",
    ))
    return items


def _timeseries(
    case: ProjectCase,
    microgrid: MicrogridData,
    baseline: OptimizationResult,
    optimized: OptimizationResult,
) -> list[TimeSeriesPoint]:
    base = baseline.microgrids[microgrid.name]
    opt = optimized.microgrids[microgrid.name]
    voltage = np.asarray(opt["voltage_pu"], dtype=float)
    loading = np.asarray(opt["line_loading_pu"], dtype=float)
    load_p = np.sum(np.vstack(list(microgrid.load_p_mw.values())), axis=0)
    load_q = np.sum(np.vstack(list(microgrid.load_q_mvar.values())), axis=0)
    energy = np.asarray(opt["storage_energy_mwh"], dtype=float)
    floor = np.asarray(opt["p_grid_security_floor_mw"], dtype=float)
    headroom = np.asarray(opt["p_grid_security_headroom_mw"], dtype=float)

    points: list[TimeSeriesPoint] = []
    for index, hour in enumerate(case.time_hours):
        points.append(TimeSeriesPoint(
            time_hour=float(hour),
            price_cny_per_mwh=float(case.price_cny_per_mwh[index]),
            load_mw=float(load_p[index]),
            load_mvar=float(load_q[index]),
            p_grid_baseline_mw=float(base["p_grid_mw"][index]),
            p_grid_optimized_mw=float(opt["p_grid_mw"][index]),
            q_grid_optimized_mvar=float(opt["q_grid_mvar"][index]),
            power_factor=float(opt["power_factor"][index]),
            wind_available_mw=float(opt["wind_available_mw"][index]),
            wind_used_mw=float(opt["wind_used_mw"][index]),
            pv_available_mw=float(opt["pv_available_mw"][index]),
            pv_used_mw=float(opt["pv_used_mw"][index]),
            storage_charge_mw=float(opt["storage_charge_mw"][index]),
            storage_discharge_mw=float(opt["storage_discharge_mw"][index]),
            storage_energy_mwh=float(energy[index + 1]),
            svg_q_mvar=float(opt["svg_q_mvar"][index]),
            minimum_voltage_pu=float(np.min(voltage[:, index])),
            maximum_voltage_pu=float(np.max(voltage[:, index])),
            maximum_line_loading_pu=float(np.max(loading[:, index])),
            active_loss_mw=float(opt["loss_mw"][index]),
            p_grid_security_floor_mw=float(floor[index]),
            p_grid_security_headroom_mw=float(headroom[index]),
        ))
    return points


def _run_single_microgrid(
    request: SimulationRequest,
    progress_callback: ProgressCallback | None = None,
    *, input_case: ProjectCase | None = None,
) -> SimulationResult:
    """运行真实单微网基准/优化求解并返回网页稳定契约。"""
    _emit(progress_callback, SimulationStage.PREPARING_CASE, "构造参数化模拟算例", 1)
    case = apply_physical_events(
        input_case if input_case is not None else build_synthetic_case(steps=request.steps),
        request.events,
    )
    lookup = {microgrid.name: microgrid for microgrid in case.microgrids}
    microgrid = lookup[request.region]
    names = [request.region]

    _emit(progress_callback, SimulationStage.SOLVING_BASELINE, "求解单微网基准方案", 2)
    baseline, baseline_details = _solve(
        case,
        names,
        formulation=request.solver.formulation,
        storage_enabled=False,
        time_limit_seconds=request.solver.time_limit_seconds,
    )
    _require_success("single baseline", baseline)

    _emit(progress_callback, SimulationStage.SOLVING_OPTIMIZED, "求解单微网优化方案", 3)
    optimized, optimized_details = _solve(
        case,
        names,
        formulation=request.solver.formulation,
        storage_enabled=request.storage_enabled,
        time_limit_seconds=request.solver.time_limit_seconds,
    )
    _require_success("single optimized", optimized)

    _emit(progress_callback, SimulationStage.VALIDATING, "执行约束与独立 AC 校核", 4)
    comparison = compare_results(baseline, optimized)
    validation = validate_result(case, optimized, names)
    ac_validation = validate_ac_dispatch(case, optimized, names)
    items = _validation_items(request.region, validation, ac_validation)
    overall_passed = bool(validation["passed"] and ac_validation["passed"] and all(item.passed for item in items))

    _emit(progress_callback, SimulationStage.SERIALIZING, "整理网页结构化结果", 5)
    nodes, edges = _topology(microgrid)
    points = _timeseries(case, microgrid, baseline, optimized)
    local = validation["microgrids"][request.region]
    result = SimulationResult(
        economic_accounting_version=ACCOUNTING_VERSION,
        metadata=SimulationMetadata(
            generated_at=datetime.now(timezone.utc).isoformat(),
            scenario_type=request.scenario_type,
            region=request.region,
            steps=request.steps,
            dt_hours=case.assumptions.dt_hours,
            formulation=request.solver.formulation,
        ),
        request=request,
        executive_summary=ExecutiveSummary(
            overall_passed=overall_passed,
            headline="单微电网优化及独立 AC 校核通过"
            if overall_passed
            else "单微电网存在未通过的校核项",
            economic_improvement_percent=float(comparison["economic_improvement_percent"]),
            baseline_economic_cost_cny=float(comparison["baseline_economic_cost_cny"]),
            optimized_economic_cost_cny=float(comparison["optimized_economic_cost_cny"]),
            optimized_peak_import_mw=float(comparison["optimized_peak_import_mw"]),
            total_active_loss_mwh=float(optimized.cluster["total_active_loss_mwh"]),
            minimum_power_factor=float(local["minimum_power_factor"]),
            minimum_voltage_pu=float(local["minimum_voltage_pu"]),
            maximum_voltage_pu=float(local["maximum_voltage_pu"]),
        ),
        topology_nodes=nodes,
        topology_edges=edges,
        validation_items=items,
        comparison={key: float(value) for key, value in comparison.items()},
        solver={
            "success": optimized.success,
            "status": optimized.status,
            "message": optimized.message,
            "mip_gap": optimized.mip_gap,
            "model_size": optimized.model_size,
            "baseline": baseline_details,
            "optimized": optimized_details,
        },
        timeseries=points,
    )
    _emit(progress_callback, SimulationStage.SUCCEEDED, "仿真与校核完成", 6)
    return result


def _combined_topology(case: ProjectCase, names: list[str]) -> tuple[list[TopologyNode], list[TopologyEdge]]:
    lookup = {microgrid.name: microgrid for microgrid in case.microgrids}
    nodes: list[TopologyNode] = []
    edges: list[TopologyEdge] = []
    for name in names:
        local_nodes, local_edges = _topology(lookup[name])
        nodes.extend(local_nodes)
        edges.extend(local_edges)
    return nodes, edges


def _cluster_timeseries(
    case: ProjectCase,
    names: list[str],
    optimized: OptimizationResult,
    aggregate_import_mw: np.ndarray,
    aggregate_q_mvar: np.ndarray,
    regional_import_mw: dict[str, np.ndarray],
) -> list[ClusterTimeSeriesPoint]:
    lookup = {microgrid.name: microgrid for microgrid in case.microgrids}
    aggregate_load = np.sum(np.vstack([
        np.sum(np.vstack(list(lookup[name].load_p_mw.values())), axis=0)
        for name in names
    ]), axis=0)
    aggregate_renewable = np.sum(np.vstack([
        np.asarray(optimized.microgrids[name]["wind_used_mw"])
        + np.asarray(optimized.microgrids[name]["pv_used_mw"])
        for name in names
    ]), axis=0)
    return [
        ClusterTimeSeriesPoint(
            time_hour=float(hour),
            aggregate_load_mw=float(aggregate_load[index]),
            aggregate_renewable_mw=float(aggregate_renewable[index]),
            aggregate_import_mw=float(aggregate_import_mw[index]),
            aggregate_q_mvar=float(aggregate_q_mvar[index]),
            import_limit_mw=float(case.cluster_import_limit_mw),
            regional_import_mw={
                name: float(regional_import_mw[name][index]) for name in names
            },
        )
        for index, hour in enumerate(case.time_hours)
    ]


def _admm_history(result: Any) -> list[ADMMHistoryPoint]:
    return [
        ADMMHistoryPoint(
            iteration=item.iteration,
            primal_residual=item.primal_residual,
            dual_residual=item.dual_residual,
            primal_tolerance=item.primal_tolerance,
            dual_tolerance=item.dual_tolerance,
            fresh_region_count=item.fresh_region_count,
            convergence_streak=item.convergence_streak,
            fallback_regions=list(item.fallback_regions),
        )
        for item in result.history
    ]


def _communication_summary(
    result: Any,
    config: CommunicationConfig,
) -> CommunicationSummary:
    metrics = result.communication
    return CommunicationSummary(
        sent=metrics.sent,
        delivered=metrics.delivered,
        dropped=metrics.dropped,
        delayed=metrics.delayed,
        outage_dropped=metrics.outage_dropped,
        stale_uses=metrics.stale_uses,
        fallback_uses=metrics.fallback_uses,
        loss_probability=config.loss_probability,
        loss_start_iteration=config.loss_start_iteration,
        loss_end_iteration=config.loss_end_iteration,
        outage_region=config.outage_region,
        outage_start_iteration=config.outage_start_iteration,
        outage_end_iteration=config.outage_end_iteration,
        event_executions=metrics.event_executions,
    )


def _run_cluster(
    request: SimulationRequest,
    progress_callback: ProgressCallback | None,
    *,
    communication_fault: bool,
    input_case: ProjectCase | None = None,
) -> SimulationResult:
    _emit(progress_callback, SimulationStage.PREPARING_CASE, "构造三区域参数化模拟算例", 1)
    case = apply_physical_events(
        input_case if input_case is not None else build_synthetic_case(steps=request.steps),
        request.events,
    )
    names = [microgrid.name for microgrid in case.microgrids]
    lookup = {microgrid.name: microgrid for microgrid in case.microgrids}
    capabilities = DispatchCapabilities(storage_enabled=request.storage_enabled)

    _emit(progress_callback, SimulationStage.SOLVING_BASELINE, "求解集群无储能基准方案", 2)
    baseline, baseline_details = _solve(
        case,
        names,
        formulation=request.solver.formulation,
        storage_enabled=False,
        time_limit_seconds=request.solver.time_limit_seconds,
        cluster_coordination=True,
    )
    _require_success("cluster baseline", baseline)

    _emit(progress_callback, SimulationStage.SOLVING_OPTIMIZED, "求解集中式参考与分布式协调", 3)
    optimized, optimized_details = _solve(
        case,
        names,
        formulation=request.solver.formulation,
        storage_enabled=capabilities.storage_enabled,
        time_limit_seconds=request.solver.time_limit_seconds,
        cluster_coordination=True,
    )
    _require_success("cluster optimized", optimized)
    loss_calibration = {
        name: {
            "p_loss_mw": np.asarray(optimized.microgrids[name]["loss_mw"]),
            "q_loss_mvar": np.asarray(optimized.microgrids[name]["reactive_loss_mvar"]),
        }
        for name in names
    }
    if communication_fault:
        outage_events = [
            event for event in request.events
            if event.event_type is EventType.COMMUNICATION_OUTAGE
        ]
        outage = outage_events[0] if len(outage_events) == 1 else None
        loss_events = [
            event for event in request.events
            if event.event_type is EventType.COMMUNICATION_PACKET_LOSS
        ]
        loss_event = loss_events[0] if len(loss_events) == 1 else None
        communication_config = CommunicationConfig(
            loss_probability=loss_event.magnitude
            if loss_event
            else 0.0
            if loss_events
            else request.communication_loss_probability,
            loss_start_iteration=int(loss_event.start) if loss_event else None,
            loss_end_iteration=int(loss_event.end) if loss_event else None,
            min_delay_iterations=0,
            max_delay_iterations=request.communication_max_delay_iterations,
            stale_limit_iterations=3,
            outage_region=outage.target
            if outage
            else None
            if outage_events
            else request.communication_outage_region,
            outage_start_iteration=int(outage.start)
            if outage
            else None
            if outage_events
            else request.communication_outage_start_iteration,
            outage_end_iteration=int(outage.end)
            if outage
            else None
            if outage_events
            else request.communication_outage_end_iteration,
            random_seed=20260906,
            events=compile_communication_events(request.events),
        )
    else:
        communication_config = CommunicationConfig()
    admm = run_admm_coordination(
        case,
        names,
        admm_config=ADMMConfig(max_iterations=request.admm_max_iterations),
        communication_config=communication_config,
        loss_calibration=loss_calibration,
        storage_enabled=capabilities.storage_enabled,
    )

    _emit(progress_callback, SimulationStage.VALIDATING, "执行集群约束与独立 AC 校核", 4)
    comparison = compare_results(baseline, optimized)
    validation = validate_result(case, optimized, names)
    ac_validation = validate_ac_dispatch(case, optimized, names)
    items = [
        item.model_copy(update={"validation_basis": "centralized_reference"})
        for name in names
        for item in _validation_items(name, validation, ac_validation)
    ]
    plan_times = tuple(float(t * 60) for t in case.time_hours)
    reference_limit = assess_cluster_import(
        stage="admm_reference",
        expected_regions=names,
        trajectories=tuple(
            RegionalImport(
                name,
                plan_times,
                tuple(float(v) for v in admm.p_references_mw[name]),
                (bool(admm.converged),) * len(plan_times),
            )
            for name in names
        ),
        limit_mw=case.cluster_import_limit_mw,
    )
    centralized_limit = assess_cluster_import(
        stage="centralized_reference",
        expected_regions=names,
        trajectories=tuple(
            RegionalImport(
                name,
                plan_times,
                tuple(float(v) for v in optimized.microgrids[name]["p_grid_mw"]),
                (bool(ac_validation["passed"]),) * len(plan_times),
            )
            for name in names
        ),
        limit_mw=case.cluster_import_limit_mw,
    )
    cluster_validation = summarize_cluster_validation(
        (
            centralized_limit,
            reference_limit,
            *(
                assess_cluster_import(
                    stage=stage,
                    expected_regions=names,
                    trajectories=(),
                    limit_mw=case.cluster_import_limit_mw,
                    computed=False,
                )
                for stage in ("day_ahead", "intraday", "minute")
            ),
        ),
        scope="reference_only",
    )
    cluster_limit_passed = cluster_validation.assessed_scope_status == "passed"
    items.append(
        ValidationItem(
            code="CLUSTER_IMPORT_LIMIT",
            label="集群受电通道容量",
            passed=reference_limit.status == "passed",
            actual=reference_limit.peak_import_mw,
            limit=float(case.cluster_import_limit_mw),
            unit="MW",
            scope="cluster",
            explanation="三区域协调参考的总受电功率不超过集群通道上限",
            validation_basis="admm_reference",
        )
    )
    items.append(ValidationItem(
        code="ADMM_CONVERGENCE",
        label="分布式协调收敛",
        passed=bool(admm.converged),
        actual=admm.stop_reason,
        limit="residual_tolerances_met",
        scope="cluster",
        explanation="ADMM 原始残差和对偶残差连续满足收敛容差",
        validation_basis="admm_reference",
    ))
    if communication_config.events:
        covered = all(item.status == "executed" for item in admm.communication.event_executions)
        items.append(
            ValidationItem(
                code="COMMUNICATION_EVENTS_COVERED",
                label="请求的通信事件窗口已完整执行",
                passed=covered,
                actual=covered,
                limit=True,
                scope="communication_events",
                explanation="逐事件记录实际覆盖轮次；提前收敛未到达的窗口不计为故障验证通过。",
                validation_basis="admm_reference",
            )
        )
    overall_passed = bool(
        validation["passed"]
        and ac_validation["passed"]
        and admm.converged
        and cluster_limit_passed
        and all(item.passed for item in items)
    )

    _emit(progress_callback, SimulationStage.SERIALIZING, "整理集群协调与通信轨迹", 5)
    nodes, edges = _combined_topology(case, names)
    chosen_region = lookup[request.region]
    points = _timeseries(case, chosen_region, baseline, optimized)
    surrogate_objective = float(
        sum(schedule.local_objective_cny for schedule in admm.schedules.values())
    )
    centralized_cost = float(optimized.cluster["economic_cost_cny"])
    if any(schedule.economic_cost is None for schedule in admm.schedules.values()):
        raise ValueError("ADMM reference is missing versioned economic accounting")
    reference_economics = assess_reference_economics(
        (schedule.economic_cost for schedule in admm.schedules.values()),
        centralized_economic_cost_cny=centralized_cost,
        surrogate_objective_cny=surrogate_objective,
        converged=admm.converged,
    )
    normalized_comparison = dict(comparison)
    normalized_comparison.update(
        {
            "admm_iterations": float(admm.iterations),
            "admm_coordination_updates": float(admm.coordination_updates),
            # Compatibility aliases now use the same declared economic formula.
            # The typed assessment labels their aggregate surrogate scope explicitly.
            "distributed_reference_cost_cny": reference_economics.reference.economic_cost_cny,
            "distributed_surrogate_objective_cny": surrogate_objective,
        }
    )
    if reference_economics.gap_percent is not None:
        normalized_comparison["distributed_reference_cost_gap_percent"] = (
            reference_economics.gap_percent
        )
    scenario_label = "通信故障下集群协调" if communication_fault else "三区域集群协调"
    result = SimulationResult(
        reference_economics=reference_economics,
        cluster_validation=cluster_validation,
        coordination_snapshot=admm.coordination_snapshot,
        economic_accounting_version=ACCOUNTING_VERSION,
        metadata=SimulationMetadata(
            generated_at=datetime.now(timezone.utc).isoformat(),
            scenario_type=request.scenario_type,
            region="SC,YA_B,YA_C",
            steps=request.steps,
            dt_hours=case.assumptions.dt_hours,
            formulation=request.solver.formulation,
        ),
        request=request,
        executive_summary=ExecutiveSummary(
            overall_passed=overall_passed,
            headline=f"{scenario_label}：集中式基准与协调参考校核通过（分布式执行未校核）"
            if overall_passed
            else f"{scenario_label}存在未通过项",
            economic_improvement_percent=float(comparison["economic_improvement_percent"]),
            baseline_economic_cost_cny=float(comparison["baseline_economic_cost_cny"]),
            optimized_economic_cost_cny=float(comparison["optimized_economic_cost_cny"]),
            optimized_peak_import_mw=float(comparison["optimized_peak_import_mw"]),
            total_active_loss_mwh=float(optimized.cluster["total_active_loss_mwh"]),
            minimum_power_factor=float(
                min(validation["microgrids"][name]["minimum_power_factor"] for name in names)
            ),
            minimum_voltage_pu=float(
                min(validation["microgrids"][name]["minimum_voltage_pu"] for name in names)
            ),
            maximum_voltage_pu=float(
                max(validation["microgrids"][name]["maximum_voltage_pu"] for name in names)
            ),
        ),
        topology_nodes=nodes,
        topology_edges=edges,
        validation_items=items,
        comparison={key: float(value) for key, value in normalized_comparison.items()},
        solver={
            "success": optimized.success and admm.converged,
            "status": optimized.status,
            "message": optimized.message,
            "mip_gap": optimized.mip_gap,
            "model_size": optimized.model_size,
            "baseline": baseline_details,
            "optimized": optimized_details,
            "admm_stop_reason": admm.stop_reason,
        },
        timeseries=points,
        cluster_timeseries=_cluster_timeseries(
            case,
            names,
            optimized,
            admm.aggregate_import_mw,
            admm.aggregate_q_mvar,
            admm.p_references_mw,
        ),
        admm_history=_admm_history(admm),
        communication=_communication_summary(admm, communication_config),
    )
    _emit(progress_callback, SimulationStage.SUCCEEDED, "仿真与校核完成", 6)
    return result


def _run_group_control(
    request: SimulationRequest,
    progress_callback: ProgressCallback | None,
    *, input_case: ProjectCase | None = None,
) -> SimulationResult:
    _emit(progress_callback, SimulationStage.PREPARING_CASE, "初始化光伏群控状态机", 1)
    case = input_case if input_case is not None else build_synthetic_case(steps=request.steps)
    lookup = {microgrid.name: microgrid for microgrid in case.microgrids}
    _emit(progress_callback, SimulationStage.SOLVING_BASELINE, "装载八类确定性控制场景", 2)
    _emit(progress_callback, SimulationStage.SOLVING_OPTIMIZED, "执行真实群控监督器状态转换", 3)
    scenarios = run_group_control_scenarios()

    _emit(progress_callback, SimulationStage.VALIDATING, "核验触发、防抖、限发与恢复动作", 4)
    all_passed = all(scenario.passed for scenario in scenarios.values())
    validation_items = [
        ValidationItem(
            code=f"GROUP_{name.upper()}",
            label=scenario.description,
            passed=scenario.passed,
            actual="passed" if scenario.passed else ", ".join(scenario.failed_checks),
            limit="passed",
            scope="group_control",
            explanation=f"真实状态机回归：{len(scenario.records)} 个判断周期、{len(scenario.events)} 个审计事件",
        )
        for name, scenario in scenarios.items()
    ]

    _emit(progress_callback, SimulationStage.SERIALIZING, "整理群控阈值、状态和事件轨迹", 5)
    records = [
        GroupControlRecord(
            scenario=name,
            time_minute=float(record.control_input.time_minutes),
            pcc_power_mw=float(record.control_input.pcc_power_mw),
            risk_limit_mw=float(record.decision.thresholds.risk_limit_mw),
            safety_threshold_mw=float(record.decision.thresholds.safety_threshold_mw),
            restore_threshold_mw=float(record.decision.thresholds.restore_threshold_mw),
            risk_index=float(record.decision.thresholds.risk_index),
            state=record.decision.state.value,
            action=record.decision.action.value,
            trigger_reason=record.decision.trigger_reason,
            requested_curtailment_mw=float(record.decision.requested_curtailment_mw),
            requested_restoration_mw=float(record.decision.requested_restoration_mw),
            remaining_curtailment_mw=float(record.decision.remaining_curtailment_mw),
        )
        for name, scenario in scenarios.items()
        for record in scenario.records
    ]
    events = [
        GroupControlEventRecord(
            scenario=name,
            time_minute=float(event.time_minutes),
            event_type=event.event_type,
            reason=event.reason,
            previous_state=event.previous_state.value,
            new_state=event.new_state.value,
            requested_power_mw=float(event.requested_power_mw),
            achieved_power_mw=float(event.achieved_power_mw),
        )
        for name, scenario in scenarios.items()
        for event in scenario.events
    ]
    nodes, edges = _topology(lookup[request.region])
    result = SimulationResult(
        metadata=SimulationMetadata(
            generated_at=datetime.now(timezone.utc).isoformat(),
            scenario_type=request.scenario_type,
            region=request.region,
            steps=request.steps,
            dt_hours=case.assumptions.dt_hours,
            formulation=request.solver.formulation,
        ),
        request=request,
        executive_summary=ExecutiveSummary(
            overall_passed=all_passed,
            headline="八类光伏群调群控场景全部通过" if all_passed else "光伏群控场景存在未通过项",
            economic_improvement_percent=0.0,
            baseline_economic_cost_cny=0.0,
            optimized_economic_cost_cny=0.0,
            optimized_peak_import_mw=float(max(record.pcc_power_mw for record in records)),
            total_active_loss_mwh=0.0,
            minimum_power_factor=1.0,
            minimum_voltage_pu=1.0,
            maximum_voltage_pu=1.0,
        ),
        topology_nodes=nodes,
        topology_edges=edges,
        validation_items=validation_items,
        comparison={
            "scenario_count": float(len(scenarios)),
            "passed_scenario_count": float(sum(scenario.passed for scenario in scenarios.values())),
            "event_count": float(len(events)),
        },
        solver={
            "success": all_passed,
            "status": "state_machine_completed",
            "message": "deterministic group-control scenario suite",
            "mip_gap": None,
            "model_size": None,
        },
        timeseries=[],
        group_control=GroupControlSummary(
            all_passed=all_passed,
            scenario_count=len(scenarios),
            scenario_checks={name: scenario.checks for name, scenario in scenarios.items()},
            records=records,
            events=events,
        ),
    )
    _emit(progress_callback, SimulationStage.SUCCEEDED, "群控状态机仿真完成", 6)
    return result


def run_simulation(
    request: SimulationRequest,
    progress_callback: ProgressCallback | None = None,
    *, input_case: ProjectCase | None = None,
) -> SimulationResult:
    """按场景运行现有真实求解器或群控状态机，并返回统一网页契约。"""
    # Replaying a stored request or an unvalidated model_copy is a new execution.
    request = SimulationRequest.model_validate(request.model_dump())
    validate_scenario_events(
        request.scenario_type.value,
        request.region,
        request.events,
        request.admm_max_iterations,
        available_regions=[mg.name for mg in input_case.microgrids]
        if input_case is not None
        else None,
    )
    if input_case is not None and len(input_case.time_hours) != request.steps:
        raise ValueError("explicit case length must match requested steps")
    if request.scenario_type is ScenarioType.SINGLE_MICROGRID:
        return _run_single_microgrid(request, progress_callback, input_case=input_case)
    if request.scenario_type is ScenarioType.CLUSTER_COORDINATION:
        return _run_cluster(request, progress_callback, communication_fault=False, input_case=input_case)
    if request.scenario_type is ScenarioType.COMMUNICATION_FAULT:
        return _run_cluster(request, progress_callback, communication_fault=True, input_case=input_case)
    if request.scenario_type is ScenarioType.GROUP_CONTROL:
        return _run_group_control(request, progress_callback, input_case=input_case)
    raise ValueError(f"unsupported scenario type: {request.scenario_type}")


__all__ = [
    "ADMMHistoryPoint", "ClusterTimeSeriesPoint", "CommunicationSummary",
    "ExecutiveSummary", "GroupControlEventRecord", "GroupControlRecord",
    "GroupControlSummary", "ProgressCallback", "ProgressUpdate", "ScenarioType",
    "SimulationMetadata", "SimulationRequest", "SimulationResult", "SimulationStage",
    "SolverFormulation", "SolverOptions", "TimeSeriesPoint", "TopologyEdge",
    "TopologyNode", "ValidationItem", "run_simulation",
]

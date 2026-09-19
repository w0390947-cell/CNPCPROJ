"""多运行方式/N−1静态潮流批量校核；不发控制命令、不自动搜索转供路径。

每个场景复用同一组带母线ID的净需求快照。显式指定转供运行方式时，故障支路
在故障后和转供后均保持退出。孤岛不进入单PCC潮流，不猜测失供负荷或孤岛电源。
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from enum import Enum
from math import hypot, isfinite, sqrt
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import numpy as np

from .ac_power_flow import backward_forward_sweep_resolved
from .modules.power_flow.api import validate_bus_demands
from .modules.power_flow.contracts import FlowNumerics, PointInputValidation
from .modules.studies.contracts import NetworkScenarioSummaryMetadata
from .network_model import NetworkModelV2, NetworkReadiness, ResolvedNetwork, assess_network_model


class ScenarioStatus(str, Enum):
    SECURE = "secure"
    TRANSFER_SECURE = "transfer_secure"
    VIOLATION = "violation"
    ISLANDED = "islanded"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"
    NOT_CONVERGED = "not_converged"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class NetworkOperatingPoint:
    """已归一化的负荷减设备出力；不含固定并联补偿。ID可采用真实时间戳。

    quality_valid/coherent由调用层判定。None/NaN/缺列/坏快照将逐点报告，
    不把最后一个值作为当前真实值。source应标明量测/估计/优化计划及版本。
    """

    point_id: str
    source: str
    p_demand_mw_by_bus: Mapping[str, float | None]
    q_demand_mvar_by_bus: Mapping[str, float | None]
    quality_valid: bool
    coherent: bool

    def __post_init__(self) -> None:
        if not self.point_id.strip() or not self.source.strip():
            raise ValueError("point_id and source are required")
        for name in ("p_demand_mw_by_bus", "q_demand_mvar_by_bus"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class NetworkSecurityLimits:
    """调用者显式给出研究范围内限值；核心不猜测现场定值。"""

    voltage_min_pu: float
    voltage_max_pu: float
    pcc_import_min_mw: float
    pcc_import_max_mw: float
    power_factor_min: float
    comparison_tolerance: float = 1e-6
    flow_tolerance: float = 1e-10
    max_iterations: int = 100
    power_tolerance_pu: float = 1e-8
    singular_voltage_pu: float = 1e-8

    def __post_init__(self) -> None:
        FlowNumerics(self.flow_tolerance, self.power_tolerance_pu, self.singular_voltage_pu)
        numbers = (self.voltage_min_pu, self.voltage_max_pu, self.pcc_import_min_mw,
                   self.pcc_import_max_mw, self.power_factor_min,
                   self.comparison_tolerance, self.flow_tolerance)
        if not all(isfinite(value) for value in numbers):
            raise ValueError("security limits must be finite")
        if not 0 < self.voltage_min_pu < self.voltage_max_pu:
            raise ValueError("invalid voltage limits")
        if self.pcc_import_min_mw > self.pcc_import_max_mw:
            raise ValueError("invalid PCC import limits")
        if not 0 <= self.power_factor_min <= 1:
            raise ValueError("invalid power factor limit")
        if self.comparison_tolerance < 0 or self.flow_tolerance <= 0:
            raise ValueError("invalid numeric tolerance")
        if type(self.max_iterations) is not int or self.max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")


@dataclass(frozen=True)
class NetworkScenario:
    scenario_id: str
    operating_mode_id: str
    contingency_id: str | None = None
    recovery_operating_mode_id: str | None = None

    def __post_init__(self) -> None:
        if not self.scenario_id.strip() or not self.operating_mode_id.strip():
            raise ValueError("scenario and operating mode IDs are required")
        if self.recovery_operating_mode_id is not None and self.contingency_id is None:
            raise ValueError("a recovery mode requires an explicit contingency")


@dataclass(frozen=True)
class SecurityViolation:
    code: str
    asset_id: str
    actual: float
    limit: float


@dataclass(frozen=True)
class BusFlowResult:
    bus_id: str
    voltage_pu: float
    fixed_shunt_q_mvar: float
    voltage_kv: float | None = None


@dataclass(frozen=True)
class BranchFlowResult:
    branch_id: str
    parent_bus_id: str
    child_bus_id: str
    tap_ratio: float
    sending_p_mw: float
    sending_q_mvar: float
    receiving_p_mw: float
    receiving_q_mvar: float
    loading_pu: float
    active_loss_mw: float
    reactive_loss_mvar: float
    kind: str | None = None
    sending_current_a: float | None = None
    receiving_current_a: float | None = None


@dataclass(frozen=True)
class PointFlowResult:
    point_id: str
    source: str
    status: ScenarioStatus
    reasons: tuple[str, ...] = ()
    iterations: int | None = None
    pcc_import_mw: float | None = None
    pcc_reactive_mvar: float | None = None
    power_factor: float | None = None
    loss_mw: float | None = None
    reactive_loss_mvar: float | None = None
    buses: tuple[BusFlowResult, ...] = ()
    branches: tuple[BranchFlowResult, ...] = ()
    violations: tuple[SecurityViolation, ...] = ()
    maximum_power_balance_residual_pu: float | None = None


@dataclass(frozen=True)
class ScenarioStageResult:
    stage: str
    operating_mode_id: str
    contingency_id: str | None
    status: ScenarioStatus
    reasons: tuple[str, ...]
    active_branch_ids: tuple[str, ...]
    reachable_bus_ids: tuple[str, ...]
    disconnected_bus_ids: tuple[str, ...]
    points: tuple[PointFlowResult, ...] = ()
    topology_status: ScenarioStatus | None = None


@dataclass(frozen=True)
class NetworkScenarioResult:
    scenario: NetworkScenario
    outaged_branch_ids: tuple[str, ...]
    status: ScenarioStatus
    stages: tuple[ScenarioStageResult, ...]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class NetworkScenarioBatch:
    network_id: str
    data_source: str
    dataset_version: str
    synthetic: bool
    limits: NetworkSecurityLimits
    results: tuple[NetworkScenarioResult, ...]
    uncovered_n_minus_one: tuple[str, ...]
    point_inputs: tuple[PointInputValidation, ...] = ()

    @property
    def inputs_valid(self) -> bool | None:
        # Historical batches have no independent input-validation evidence.
        return all(point.valid for point in self.point_inputs) if self.point_inputs else None

    @property
    def all_final_states_secure(self) -> bool:
        applicable = [r for r in self.results if r.status is not ScenarioStatus.NOT_APPLICABLE]
        return bool(applicable) and all(
            r.status in (ScenarioStatus.SECURE, ScenarioStatus.TRANSFER_SECURE)
            for r in applicable
        )

    @property
    def all_immediate_states_secure(self) -> bool:
        applicable = [r for r in self.results if r.status is not ScenarioStatus.NOT_APPLICABLE]
        return bool(applicable) and all(
            bool(r.stages) and r.stages[0].status is ScenarioStatus.SECURE for r in applicable
        )

    @property
    def n_minus_one_coverage_complete(self) -> bool:
        # 完整覆盖与校核通过是两个不同概念。无故障场景时绝不报告覆盖完成。
        return (
            self.inputs_valid is True
            and not self.uncovered_n_minus_one
            and any(
                len(r.outaged_branch_ids) == 1 and r.status is not ScenarioStatus.NOT_APPLICABLE
                for r in self.results
            )
        )


def build_network_scenarios(
    model: NetworkModelV2,
    operating_mode_ids: Sequence[str] | None = None,
    *,
    recovery_modes: Mapping[tuple[str, str], str] | None = None,
) -> tuple[NetworkScenario, ...]:
    """每个所选运行方式的基态及全部已登记启用的N−1；转供必须显式映射。"""
    mode_ids = tuple(operating_mode_ids) if operating_mode_ids is not None else tuple(
        mode.mode_id for mode in model.operating_modes
    )
    if not mode_ids or len(set(mode_ids)) != len(mode_ids):
        raise ValueError("select at least one unique operating mode")
    recoveries = dict(recovery_modes or {})
    pairs = {(mode, c.contingency_id) for mode in mode_ids
             for c in model.contingencies if c.enabled and c.order == 1}
    if not set(recoveries).issubset(pairs):
        raise ValueError("recovery mapping references an unselected mode/contingency")
    scenarios: list[NetworkScenario] = []
    for mode in mode_ids:
        scenarios.append(NetworkScenario(f"mode:{mode}", mode))
        for contingency in model.contingencies:
            if contingency.enabled and contingency.order == 1:
                scenarios.append(NetworkScenario(
                    f"mode:{mode}/outage:{contingency.contingency_id}", mode,
                    contingency.contingency_id, recoveries.get((mode, contingency.contingency_id)),
                ))
    return tuple(scenarios)


def _point_invalid_reasons(point: NetworkOperatingPoint, bus_ids: tuple[str, ...]) -> tuple[str, ...]:
    return validate_bus_demands(
        bus_ids,
        point.p_demand_mw_by_bus,
        point.q_demand_mvar_by_bus,
        quality_valid=point.quality_valid,
        coherent=point.coherent,
    )


def evaluate_operating_point(
    network: ResolvedNetwork, point: NetworkOperatingPoint, limits: NetworkSecurityLimits,
    *, slack_voltage_pu: float = 1.0,
) -> PointFlowResult:
    """固定净注入AC校核；保留越限，缺少电压基准时工程单位量返回None。"""
    bus_ids = tuple(bus.bus_id for bus in network.buses)
    reasons = _point_invalid_reasons(point, bus_ids)
    if reasons:
        return PointFlowResult(point.point_id, point.source, ScenarioStatus.INVALID_INPUT, reasons)
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        try:
            flow = backward_forward_sweep_resolved(
                network,
                np.asarray([point.p_demand_mw_by_bus[b] for b in bus_ids]),
                np.asarray([point.q_demand_mvar_by_bus[b] for b in bus_ids]),
                slack_voltage_pu=slack_voltage_pu,
                tolerance=limits.flow_tolerance,
                max_iterations=limits.max_iterations,
                power_tolerance_pu=limits.power_tolerance_pu,
                singular_voltage_pu=limits.singular_voltage_pu,
            )
        except (FloatingPointError, OverflowError):
            return PointFlowResult(point.point_id, point.source, ScenarioStatus.NOT_CONVERGED,
                                   ("NUMERIC_FAILURE",))
    if not flow["converged"]:
        return PointFlowResult(
            point.point_id,
            point.source,
            ScenarioStatus.NOT_CONVERGED,
            ("AC_NOT_CONVERGED", str(flow.get("stop_reason", "UNKNOWN"))),
            iterations=flow["iterations"],
            maximum_power_balance_residual_pu=flow.get("maximum_power_balance_residual_pu"),
        )
    buses = tuple(BusFlowResult(
        bus_id, float(flow["voltage_pu"][i]), float(flow["fixed_shunt_q_mvar_by_bus"][bus_id]),
        (float(flow["voltage_pu"][i]) * network.buses[i].nominal_voltage_kv
         if network.buses[i].nominal_voltage_kv is not None else None),
    ) for i, bus_id in enumerate(bus_ids))
    voltage_kv = {bus.bus_id: bus.voltage_kv for bus in buses}

    def terminal_current(p: float, q: float, bus: str) -> float | None:
        voltage = voltage_kv[bus]
        return 1000 * hypot(p, q) / (sqrt(3) * voltage) if voltage is not None and voltage > 0 else None

    branches = tuple(BranchFlowResult(
        line.branch_id, line.parent, line.child, line.tap_ratio,
        float(flow["line_sending_p_mw"][i]), float(flow["line_sending_q_mvar"][i]),
        float(flow["line_receiving_p_mw"][i]), float(flow["line_receiving_q_mvar"][i]),
        float(flow["line_loading_pu"][i]), float(flow["line_p_loss_mw"][i]),
        float(flow["line_q_loss_mvar"][i]),
        line.kind.value,
        terminal_current(float(flow["line_sending_p_mw"][i]), float(flow["line_sending_q_mvar"][i]), line.parent),
        terminal_current(float(flow["line_receiving_p_mw"][i]), float(flow["line_receiving_q_mvar"][i]), line.child),
    ) for i, line in enumerate(network.branches))
    p, q = float(flow["pcc_p_mw"]), float(flow["pcc_q_mvar"])
    pf = abs(p) / hypot(p, q) if hypot(p, q) > 1e-12 else 1.0
    violations: list[SecurityViolation] = []
    eps = limits.comparison_tolerance
    for bus in buses:
        if bus.voltage_pu < limits.voltage_min_pu - eps:
            violations.append(SecurityViolation("VOLTAGE_LOW", bus.bus_id, bus.voltage_pu, limits.voltage_min_pu))
        if bus.voltage_pu > limits.voltage_max_pu + eps:
            violations.append(SecurityViolation("VOLTAGE_HIGH", bus.bus_id, bus.voltage_pu, limits.voltage_max_pu))
    for branch in branches:
        if branch.loading_pu > 1.0 + eps:
            violations.append(SecurityViolation("BRANCH_OVERLOAD", branch.branch_id, branch.loading_pu, 1.0))
    for condition, code, actual, limit in (
        (p < limits.pcc_import_min_mw - eps, "PCC_IMPORT_LOW", p, limits.pcc_import_min_mw),
        (p > limits.pcc_import_max_mw + eps, "PCC_IMPORT_HIGH", p, limits.pcc_import_max_mw),
        (pf < limits.power_factor_min - eps, "PCC_POWER_FACTOR_LOW", pf, limits.power_factor_min),
    ):
        if condition:
            violations.append(SecurityViolation(code, network.pcc_bus_id, actual, limit))
    return PointFlowResult(
        point.point_id,
        point.source,
        ScenarioStatus.VIOLATION if violations else ScenarioStatus.SECURE,
        iterations=flow["iterations"],
        pcc_import_mw=p,
        pcc_reactive_mvar=q,
        power_factor=pf,
        loss_mw=float(flow["loss_mw"]),
        reactive_loss_mvar=float(flow["reactive_loss_mvar"]),
        buses=buses,
        branches=branches,
        violations=tuple(violations),
        maximum_power_balance_residual_pu=flow["maximum_power_balance_residual_pu"],
    )


def _stage(
    model: NetworkModelV2,
    mode_id: str,
    contingency_id: str | None,
    label: str,
    points: Sequence[NetworkOperatingPoint],
    limits: NetworkSecurityLimits,
    point_inputs: tuple[PointInputValidation, ...],
) -> ScenarioStageResult:
    readiness: NetworkReadiness = assess_network_model(model, mode_id, contingency_id=contingency_id)
    samples: tuple[PointFlowResult, ...] = ()
    reasons = readiness.blockers
    if not readiness.structurally_valid:
        status = ScenarioStatus.INVALID_INPUT
    elif readiness.disconnected_bus_ids:
        status = ScenarioStatus.ISLANDED
    elif not readiness.ready_for_current_solver:
        status = ScenarioStatus.UNSUPPORTED
    else:
        status = ScenarioStatus.SECURE
    topology_status = status
    if status is not ScenarioStatus.SECURE:
        # Preserve both topology and invalid-input evidence without running AC.
        samples = tuple(
            PointFlowResult(p.point_id, p.source, ScenarioStatus.INVALID_INPUT, p.reasons)
            for p in point_inputs
            if not p.valid
        )
        if samples:
            status = ScenarioStatus.INVALID_INPUT
    else:
        network = readiness.require_current_solver_ready()
        samples = tuple(evaluate_operating_point(network, p, limits) for p in points)
        status = ScenarioStatus.SECURE
        for candidate in (ScenarioStatus.INVALID_INPUT, ScenarioStatus.NOT_CONVERGED, ScenarioStatus.VIOLATION):
            if any(sample.status is candidate for sample in samples):
                status = candidate
                break
    return ScenarioStageResult(
        label,
        mode_id,
        contingency_id,
        status,
        reasons,
        readiness.active_branch_ids,
        readiness.reachable_bus_ids,
        readiness.disconnected_bus_ids,
        samples,
        topology_status,
    )


def evaluate_network_scenarios(
    model: NetworkModelV2,
    points: Sequence[NetworkOperatingPoint],
    scenarios: Sequence[NetworkScenario],
    limits: NetworkSecurityLimits,
) -> NetworkScenarioBatch:
    """逐场景固定注入静态校核；一个场景失败不会抹掉其他场景证据。"""
    points, scenarios = tuple(points), tuple(scenarios)
    if not points or not scenarios:
        raise ValueError("at least one operating point and scenario are required")
    if len({p.point_id for p in points}) != len(points):
        raise ValueError("operating point IDs must be unique")
    if len({s.scenario_id for s in scenarios}) != len(scenarios):
        raise ValueError("scenario IDs must be unique")
    point_inputs = tuple(
        PointInputValidation(
            point.point_id,
            point.source,
            _point_invalid_reasons(point, tuple(bus.bus_id for bus in model.buses)),
        )
        for point in points
    )
    contingencies = {item.contingency_id: item for item in model.contingencies}
    results: list[NetworkScenarioResult] = []
    for scenario in scenarios:
        contingency = contingencies.get(scenario.contingency_id)
        outaged = contingency.outaged_branch_ids if contingency else ()
        base = assess_network_model(model, scenario.operating_mode_id)
        # 判定故障是否适用，依据故障前方式；转供方式不得把停运故障变成无操作。
        if (scenario.contingency_id is not None and contingency is not None
                and contingency.enabled and base.ready_for_current_solver
                and set(outaged).issubset({b.branch_id for b in model.branches})
                and not set(outaged).intersection(base.active_branch_ids)):
            results.append(NetworkScenarioResult(scenario, outaged, ScenarioStatus.NOT_APPLICABLE,
                                                 (), ("OUTAGED_BRANCH_ALREADY_OFFLINE",)))
            continue
        first = _stage(
            model,
            scenario.operating_mode_id,
            scenario.contingency_id,
            "post_fault" if scenario.contingency_id else "base",
            points,
            limits,
            point_inputs,
        )
        stages = [first]
        if scenario.recovery_operating_mode_id is not None and first.status is not ScenarioStatus.INVALID_INPUT:
            stages.append(
                _stage(
                    model,
                    scenario.recovery_operating_mode_id,
                    scenario.contingency_id,
                    "post_transfer",
                    points,
                    limits,
                    point_inputs,
                )
            )
        status = stages[-1].status
        if len(stages) == 2 and status is ScenarioStatus.SECURE:
            status = ScenarioStatus.TRANSFER_SECURE
        results.append(NetworkScenarioResult(scenario, outaged, status, tuple(stages)))
    # 对所有参与研究的故障前运行方式，列出未登记/未提交的在运单支路退出。
    uncovered = []
    for mode_id in dict.fromkeys(s.operating_mode_id for s in scenarios):
        readiness = assess_network_model(model, mode_id)
        if not readiness.ready_for_current_solver:
            uncovered.append(f"{mode_id}:BASE_MODE_NOT_READY")
        if not any(s.operating_mode_id == mode_id and s.contingency_id is None for s in scenarios):
            uncovered.append(f"{mode_id}:BASE_CASE_NOT_REQUESTED")
        covered = {r.outaged_branch_ids[0] for r in results
                   if r.scenario.operating_mode_id == mode_id and len(r.outaged_branch_ids) == 1
                   and r.status not in (ScenarioStatus.INVALID_INPUT, ScenarioStatus.NOT_APPLICABLE)}
        uncovered.extend(f"{mode_id}:{branch}" for branch in readiness.active_branch_ids if branch not in covered)
    return NetworkScenarioBatch(
        model.network_id,
        model.provenance.source,
        model.provenance.dataset_version,
        model.provenance.synthetic,
        limits,
        tuple(results),
        tuple(uncovered),
        point_inputs,
    )


def write_network_scenario_outputs(
    output: Path,
    batch: NetworkScenarioBatch,
    *,
    model: NetworkModelV2,
    points: Sequence[NetworkOperatingPoint],
) -> None:
    """归档数据来源、限值、输入与逐场景/逐点/逐设备结果；未知数值导出null。"""
    output.mkdir(parents=True, exist_ok=True)
    payload = asdict(batch)
    payload.update(
        NetworkScenarioSummaryMetadata(
            schema_version="network-scenarios-v2",
            inputs_valid=batch.inputs_valid,
            scope="fixed_injection_balanced_radial_static_screening",
            all_final_states_secure=batch.all_final_states_secure,
            all_immediate_states_secure=batch.all_immediate_states_secure,
            n_minus_one_coverage_complete=batch.n_minus_one_coverage_complete,
            field_acceptance_certified=False,
        ).model_dump(mode="json")
    )
    (output / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
    )
    def json_value(value):
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if isinstance(value, np.generic):
            return value.item()
        raise TypeError(f"unsupported export value: {type(value)}")

    # 对坏快照的 NaN保留原因并转成null，使归档仍为严格JSON。
    def normalized(values):
        return {bus: float(value) if isinstance(value, (int, float, np.number))
                and not isinstance(value, bool) and isfinite(value) else None
                for bus, value in values.items()}
    inputs = {"network": asdict(model), "points": [dict(
        point_id=p.point_id, source=p.source, quality_valid=p.quality_valid, coherent=p.coherent,
        p_demand_mw_by_bus=normalized(p.p_demand_mw_by_bus),
        q_demand_mvar_by_bus=normalized(p.q_demand_mvar_by_bus),
        invalid_reasons=_point_invalid_reasons(p, tuple(b.bus_id for b in model.buses)),
    ) for p in points]}
    (output / "inputs.json").write_text(json.dumps(
        inputs, ensure_ascii=False, indent=2, allow_nan=False, default=json_value,
    ), encoding="utf-8")
    rows = []
    buses = []
    branches = []
    for result in batch.results:
        for stage in result.stages:
            for sample in stage.points:
                key = dict(scenario_id=result.scenario.scenario_id, stage=stage.stage,
                           operating_mode_id=stage.operating_mode_id,
                           contingency_id=stage.contingency_id, point_id=sample.point_id)
                rows.append(dict(key, status=sample.status.value, pcc_import_mw=sample.pcc_import_mw,
                                 pcc_reactive_mvar=sample.pcc_reactive_mvar, loss_mw=sample.loss_mw,
                                 reasons=";".join(sample.reasons),
                                 violations=";".join(f"{v.code}:{v.asset_id}" for v in sample.violations)))
                buses.extend(dict(key, **asdict(item)) for item in sample.buses)
                branches.extend(dict(key, **asdict(item)) for item in sample.branches)
            if not stage.points or stage.topology_status in (
                ScenarioStatus.ISLANDED,
                ScenarioStatus.UNSUPPORTED,
            ):
                rows.append(
                    dict(
                        scenario_id=result.scenario.scenario_id,
                        stage=stage.stage,
                        operating_mode_id=stage.operating_mode_id,
                        contingency_id=stage.contingency_id,
                        point_id=None,
                        status=(stage.topology_status or stage.status).value,
                        pcc_import_mw=None,
                        pcc_reactive_mvar=None,
                        loss_mw=None,
                        reasons=";".join(stage.reasons),
                        violations="",
                    )
                )
        if not result.stages:
            rows.append(dict(scenario_id=result.scenario.scenario_id, stage="not_evaluated",
                             operating_mode_id=result.scenario.operating_mode_id,
                             contingency_id=result.scenario.contingency_id, point_id=None,
                             status=result.status.value, pcc_import_mw=None, pcc_reactive_mvar=None,
                             loss_mw=None, reasons=";".join(result.reasons), violations=""))
    key_fields = ["scenario_id", "stage", "operating_mode_id", "contingency_id", "point_id"]
    for filename, values, fields in (
        ("points.csv", rows, key_fields + ["status", "pcc_import_mw", "pcc_reactive_mvar", "loss_mw", "reasons", "violations"]),
        ("buses.csv", buses, key_fields + list(BusFlowResult.__dataclass_fields__)),
        ("branches.csv", branches, key_fields + list(BranchFlowResult.__dataclass_fields__)),
    ):
        with (output / filename).open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(values)

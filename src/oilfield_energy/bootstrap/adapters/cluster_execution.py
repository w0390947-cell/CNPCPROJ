"""Bridge existing numerical engines to the automatic execution workflow.

ADMM and joint network restoration retain distinct reference evidence. Each
stage applies the same strict regional and joint adoption checks.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import cast

import numpy as np
from numpy.typing import NDArray

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.admm import run_admm_coordination
from oilfield_energy.analysis import validate_result
from oilfield_energy.data import ProjectCase
from oilfield_energy.hierarchy_types import (
    ADMMConfig,
    ADMMResult,
    ClusterReference,
    CommunicationConfig,
)
from oilfield_energy.model import OptimizationResult
from oilfield_energy.modules.control.contracts import (
    BusSeries,
    DynamicTrackingPolicy,
    PlantInputs,
)
from oilfield_energy.modules.dispatch.api import (
    assess_storage_reachability,
    build_coordination_calibration,
    cluster_import_tracking_margin,
    pq_witness_violation,
)
from oilfield_energy.modules.dispatch.contracts import (
    ACRecoveryPolicy,
    PQPlanningWitness,
    CoordinationNetworkCalibration,
    CoordinationRecoveryPolicy,
    NetworkCandidatePolicy,
    NetworkCoordinationPolicy,
    ReactivePlanningPolicy,
    RegionalNetworkCandidate,
    StorageReachabilityPolicy,
    StorageReservePolicy,
    ReserveDeliveryPolicy,
)
from oilfield_energy.modules.power_flow.contracts import ClusterPowerFactorPolicy
from oilfield_energy.modules.studies.contracts import ScenarioEvent
from oilfield_energy.planning_security import build_planning_security_trajectories
from oilfield_energy.resource_control_contracts import ResourceSchedule
from oilfield_energy.runtime.parallel import OwnedProcessPool
from oilfield_energy.scenario_events import apply_physical_events
from oilfield_energy.workflows.cluster_execution.api import (
    combined_status,
    verify_execution,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    ClusterExecution,
    CoordinationRecoveryReport,
    ExecutionCheck,
    ExecutionPolicy,
    ExecutionStageName,
    RegionalCalibrationFeedback,
    RegionalExecution,
    RollingProgress,
    ClusterSafetyPolicy,
)

from .cluster_recovery import solve_with_reference_recovery
from .cluster_rolling import RollingClusterEngine, with_storage_reachability
from .project_dataset import build_synthetic_case, plant_inputs_by_region


def _values(values: np.ndarray) -> tuple[float, ...]:
    return tuple(float(v) for v in values)


def _array(data: Mapping[str, object], key: str, length: int) -> np.ndarray:
    value = data[key]
    if not isinstance(value, np.ndarray):
        raise ValueError(f"invalid numerical evidence: {key}")
    value = np.asarray(cast(NDArray[np.float64], value), dtype=np.float64)
    if value.shape != (length,) or not np.isfinite(value).all():
        raise ValueError(f"invalid numerical evidence: {key}")
    return value


def _check(
    code: str,
    label: str,
    passed: bool,
    *,
    actual: float | None = None,
    limit: float | None = None,
    unit: str = "",
) -> ExecutionCheck:
    return ExecutionCheck(
        code=code,
        label=label,
        status="passed" if passed else "violated",
        actual=actual,
        limit=limit,
        unit=unit,
    )


def _tracking(
    p: np.ndarray,
    q: np.ndarray,
    target_p: np.ndarray,
    target_q: np.ndarray,
    policy: ExecutionPolicy,
) -> list[ExecutionCheck]:
    dp = float(np.abs(p - target_p).max())
    dq = float(np.abs(q - target_q).max())
    return [
        _check(
            "P_TRACKING",
            "有功目标最大偏差",
            policy.tracking_limits.accepts_p(dp),
            actual=dp,
            limit=policy.p_tracking_tolerance_mw,
            unit="MW",
        ),
        _check(
            "Q_TRACKING",
            "无功目标最大偏差",
            policy.tracking_limits.accepts_q(dq),
            actual=dq,
            limit=policy.q_tracking_tolerance_mvar,
            unit="Mvar",
        ),
    ]


def _persistent_plant(case: ProjectCase) -> dict[str, PlantInputs]:
    """Explicit held-profile scenario for captured cases without newer inputs."""
    minutes = case.assumptions.dt_hours * 60
    if not float(minutes).is_integer():
        raise ValueError("one-minute execution requires integral planning intervals")
    result: dict[str, PlantInputs] = {}
    for mg in case.microgrids:

        def series(
            values: dict[str, np.ndarray], buses: list[str]
        ) -> tuple[BusSeries, ...]:
            return tuple(
                BusSeries(
                    bus_id=bus,
                    values=_values(
                        values.get(bus, np.zeros(len(case.time_hours))).repeat(
                            int(minutes)
                        )
                    ),
                )
                for bus in buses
            )

        result[mg.name] = PlantInputs(
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            step_minutes=1,
            load_p=series(mg.load_p_mw, mg.buses),
            load_q=series(mg.load_q_mvar, mg.buses),
            wind_available=series(mg.wind_available_mw, list(mg.wind_available_mw)),
            pv_available=series(mg.pv_available_mw, list(mg.pv_available_mw)),
        )
    return result


def _execution_inputs(
    case: ProjectCase, packaged: bool, events: list[ScenarioEvent]
) -> tuple[ProjectCase, dict[str, PlantInputs], str]:
    if not packaged:
        return (
            case,
            _persistent_plant(case),
            "捕获算例持续性场景：日内沿用原预测，分钟输入逐段保持；未模拟预测更新",
        )
    intraday = build_synthetic_case(len(case.time_hours), profile_kind="intraday")
    if intraday.dataset_sha256 != case.dataset_sha256:
        raise ValueError("day-ahead and intraday dataset identity differs")
    plants = plant_inputs_by_region()
    # Apply the same physical event windows at the native minute time axis.
    minute_case = replace(
        intraday,
        time_hours=np.arange(1440, dtype=float) / 60,
        microgrids=[
            replace(
                mg,
                **{
                    field: {
                        s.bus_id: np.asarray(s.values)
                        for s in getattr(plants[mg.name], source)
                    }
                    for field, source in (
                        ("load_p_mw", "load_p"),
                        ("load_q_mvar", "load_q"),
                        ("wind_available_mw", "wind_available"),
                        ("pv_available_mw", "pv_available"),
                    )
                },
            )
            for mg in intraday.microgrids
        ],
    )
    minute_case = apply_physical_events(minute_case, events)
    for mg in minute_case.microgrids:
        plants[mg.name] = plants[mg.name].model_copy(
            update={
                target: tuple(
                    BusSeries(bus_id=bus, values=_values(values))
                    for bus, values in getattr(mg, field).items()
                )
                for field, target in (
                    ("load_p_mw", "load_p"),
                    ("load_q_mvar", "load_q"),
                    ("wind_available_mw", "wind_available"),
                    ("pv_available_mw", "pv_available"),
                )
            }
        )
    return (
        apply_physical_events(intraday, events),
        plants,
        "同版本模拟数据集：日前预测、日内更新与原生一分钟模拟输入；同一事件窗口",
    )


def execution_inputs(
    case: ProjectCase,
    *,
    packaged: bool,
    events: list[ScenarioEvent],
) -> tuple[ProjectCase, dict[str, PlantInputs], str]:
    """Prepare explicitly labeled forecast and plant inputs for either workflow."""
    return _execution_inputs(case, packaged, events)


@dataclass(frozen=True)
class RegionalPlanRequest:
    """Owned regional inputs; contains no live engine or solver object."""

    case: ProjectCase
    name: str
    # Both absent means autonomous optimization, with no upper-level reference.
    p_target_mw: NDArray[np.float64] | None
    q_target_mvar: NDArray[np.float64] | None
    storage_enabled: bool
    seconds: float
    policy: ExecutionPolicy

    def __post_init__(self) -> None:
        if (self.p_target_mw is None) != (self.q_target_mvar is None):
            raise ValueError("P/Q references must both be present or both absent")
        if self.storage_enabled and self.policy.storage_reachability is not None:
            item = self.case.storage_reachability.get(self.name)
            if item is None or item.policy != self.policy.storage_reachability:
                raise ValueError(
                    "regional planning requires its declared storage reachability inputs"
                )


def realize_regional_plan(
    request: RegionalPlanRequest,
) -> tuple[RegionalExecution, OptimizationResult | None]:
    """Solve and certify one region without mutating orchestration state."""
    return _solve_regional_plan(request, diagnostic=False)


def diagnose_regional_reference(
    item: tuple[RegionalPlanRequest, int],
) -> RegionalCalibrationFeedback:
    """Find an AC-certified physical witness with soft reference preference only.

    No P/Q tracking certificate is issued; only a later strict realization may
    become executable. All other checks use the same implementation as adoption.
    """
    request, number = item
    evidence, result = _solve_regional_plan(request, diagnostic=True)
    calibration = None
    if result is not None:
        data = result.microgrids[request.name]
        calibration = build_coordination_calibration(
            p_loss_mw=_array(data, "loss_mw", len(request.case.time_hours)),
            q_loss_mvar=_array(
                data, "reactive_loss_mvar", len(request.case.time_hours)
            ),
            shunt_nominal_mvar_by_bus=cast(
                Mapping[str, float],
                data["fixed_shunt_q_nominal_mvar_by_bus"],
            ),
            shunt_q_mvar_by_bus=cast(
                Mapping[str, NDArray[np.float64]],
                data["fixed_shunt_q_mvar_by_bus"],
            ),
        )
    return RegionalCalibrationFeedback(
        reference_round=number,
        evidence=evidence,
        calibration=calibration,
    )


def _solve_regional_plan(
    request: RegionalPlanRequest,
    *,
    diagnostic: bool,
) -> tuple[RegionalExecution, OptimizationResult | None]:
    case, name = request.case, request.name
    targets = (
        {"p_mw": request.p_target_mw, "q_mvar": request.q_target_mvar}
        if request.p_target_mw is not None and request.q_target_mvar is not None
        else None
    )
    security = build_planning_security_trajectories(case, [name])
    checked = solve_case_ac_consistent(
        case,
        [name],
        storage_enabled=request.storage_enabled,
        cluster_coordination=False,
        pcc_targets={name: targets} if targets is not None else None,
        tracking_limits=None
        if diagnostic or targets is None
        else request.policy.tracking_limits,
        time_limit_seconds=request.seconds,
        quality_policy=request.policy.computation_quality,
        recovery_policy=request.policy.ac_recovery,
        p_grid_security_floors_mw={name: security[name].effective_floor_mw},
    )
    if not checked.optimization.success:
        infeasible = checked.stop_reason == "solver_infeasible"
        reason = (
            (
                "当前名义 P/Q 联合过渡构造不可行（不证明真实设备所有过渡不可行）"
                if case.joint_pq_plans
                else "独立单微电网当前规划模型不可行"
                if targets is None and not diagnostic
                else "区域物理诊断模型不可行"
                if diagnostic
                else "当前设备、电网和逐时段 P/Q 偏差约束下，区域目标不可行"
            )
            if infeasible
            else "区域求解未获得可用计划（不代表已证明目标不可行）"
        )
        reason += f"：{checked.stop_reason}；{checked.optimization.message}"
        evidence = RegionalExecution(
            region=name,
            optimization_quality=checked.optimization_quality,
            ac_recovery=checked.recovery,
            status="violated" if infeasible else "unknown",
            reason=reason,
            solver_stop_reason=checked.stop_reason,
            checks=(
                ExecutionCheck(
                    code="DEVICE_PLAN",
                    label="设备与运行约束（诊断）"
                    if diagnostic
                    else "设备与运行约束（含逐时段目标偏差）",
                    status="violated" if infeasible else "unknown",
                    reason=reason,
                ),
            ),
        )
        return evidence, None
    result = checked.optimization
    data = result.microgrids[name]
    length = len(case.time_hours)
    p, q = (_array(data, key, length) for key in ("p_grid_mw", "q_grid_mvar"))
    charge, discharge = (
        _array(data, key, length)
        for key in ("storage_charge_mw", "storage_discharge_mw")
    )
    energy = _array(data, "storage_energy_mwh", length + 1)
    cost = data.get("economic_cost_cny")
    if not isinstance(cost, (float, int)) or not np.isfinite(cost):
        raise ValueError("missing finite regional economic cost")
    raw_schedules = data["resource_schedules"]
    if not isinstance(raw_schedules, (tuple, list)):
        raise ValueError("missing typed device schedules")
    raw = cast(tuple[object, ...] | list[object], raw_schedules)
    if not all(isinstance(r, ResourceSchedule) for r in raw):
        raise ValueError("missing typed device schedules")
    schedules = tuple(r for r in raw if isinstance(r, ResourceSchedule))
    physical = validate_result(case, result, [name])
    ac_regions = checked.ac_validation.get("microgrids")
    ac_local = (
        cast(Mapping[str, object], ac_regions).get(name)
        if isinstance(ac_regions, dict)
        else None
    )
    ac_converged = (
        isinstance(ac_local, dict)
        and cast(Mapping[str, object], ac_local).get("all_converged") is True
    )
    checks = [
        _check("DEVICE_PLAN", "设备与运行约束", bool(physical["passed"])),
        ExecutionCheck(
            code="AC_NETWORK",
            label="独立 AC 潮流与一致性",
            status="passed"
            if checked.passed
            else "violated"
            if ac_converged
            else "unknown",
            reason="" if checked.passed else checked.stop_reason,
        ),
    ]
    if not diagnostic and targets is not None:
        checks.extend(
            _tracking(p, q, targets["p_mw"], targets["q_mvar"], request.policy)
        )
    recovered = (
        checked.passed
        and checked.recovery is not None
        and checked.recovery.outcome == "certified"
    )
    feasible_recovery = (
        recovered
        and checked.recovery is not None
        and checked.recovery.original_optimality == "unproven"
        and checked.recovery.permits_feasible_adoption
    )
    optimized_recovery = (
        recovered
        and checked.recovery is not None
        and checked.recovery.original_optimality == "proven"
    )
    if recovered:
        recovery = checked.recovery
        assert recovery is not None and recovery.selected_attempt is not None
        selected = recovery.attempts[recovery.selected_attempt - 1]
        checks.append(
            ExecutionCheck(
                code="OPTIMIZATION_QUALITY",
                label="原目标优化最优性精度",
                status="passed"
                if recovery.original_optimality == "proven"
                else "unknown",
                actual=selected.relative_gap
                if selected.phase == "original_objective"
                else None,
                limit=request.policy.computation_quality.relative_gap
                if request.policy.computation_quality
                else 1e-4,
                reason="原目标最优性已证明"
                if recovery.original_optimality == "proven"
                else "可行恢复计划；原经济目标最优性未证明",
            )
        )
    elif checked.optimization_quality:
        quality = checked.optimization_quality[-1]
        checks.append(
            ExecutionCheck(
                code="OPTIMIZATION_QUALITY",
                label="优化最优性精度",
                status="passed" if quality.status == "satisfied" else "unknown",
                actual=quality.attempts[quality.selected_attempt - 1].relative_gap,
                limit=quality.target_relative_gap,
                reason=quality.status,
            )
        )
    if not request.storage_enabled:
        checks.append(
            _check(
                "STORAGE_DISABLED",
                "储能停用约束",
                bool(
                    np.abs(charge).max() <= request.policy.numerical_tolerance
                    and np.abs(discharge).max() <= request.policy.numerical_tolerance
                ),
            )
        )
    reserve = case.storage_reserves.get(name) if request.storage_enabled else None
    if reserve is not None:
        power = discharge - charge
        mg = next(m for m in case.microgrids if m.name == name)
        storage_q = next(
            r.reactive_power_mvar
            for r in schedules
            if r.resource_id == mg.resource_id("storage", mg.storage.bus)
        )
        angles = np.arange(case.assumptions.polygon_sides) * (
            2 * np.pi / case.assumptions.polygon_sides
        )
        capacity = mg.storage.s_max_mva * np.cos(np.pi / case.assumptions.polygon_sides)
        apparent_error = max(
            0.0,
            *(
                float(
                    (
                        np.cos(angle) * (power + offset)
                        + np.sin(angle) * storage_q
                        - capacity
                    ).max()
                )
                for angle in angles
                for offset in (np.array(reserve.up_mw), -np.array(reserve.down_mw))
            ),
        )
        energy_error = float(
            max(
                0.0,
                (np.array(reserve.energy_floor_mwh) - energy).max(),
                (energy - np.array(reserve.energy_ceiling_mwh)).max(),
            )
        )
        power_error = float(
            max(
                0.0,
                (np.array(reserve.minimum_power_mw) - power).max(),
                (power - np.array(reserve.maximum_power_mw)).max(),
            )
        )
        checks.extend(
            (
                _check(
                    "STORAGE_RESERVE_CAPACITY",
                    "备用部署后的储能变流器容量",
                    apparent_error <= request.policy.numerical_tolerance,
                    actual=apparent_error,
                    limit=request.policy.numerical_tolerance,
                    unit="MVA",
                ),
                _check(
                    "STORAGE_RESERVE_ENERGY",
                    "计划储能备用电量",
                    energy_error <= request.policy.numerical_tolerance,
                    actual=energy_error,
                    limit=request.policy.numerical_tolerance,
                    unit="MWh",
                ),
                _check(
                    "STORAGE_RESERVE_POWER",
                    "计划储能双向功率备用",
                    power_error <= request.policy.numerical_tolerance,
                    actual=power_error,
                    limit=request.policy.numerical_tolerance,
                    unit="MW",
                ),
            )
        )
    reachability = None
    if request.storage_enabled and name in case.storage_reachability:
        reachability = assess_storage_reachability(
            case.storage_reachability[name],
            p_mw=_values(discharge - charge),
            energy_mwh=_values(energy),
        )
        checks.append(
            ExecutionCheck(
                code="STORAGE_REACHABILITY",
                label="储能名义动态可达性与电量递推",
                status=reachability.status,
                reason="按首个计划初始化；未证明启动前过渡"
                if reachability.inputs.initial_power_mw is None
                else "",
            )
        )
    reactive_rows = case.reactive_plans.get(name, ())
    if reactive_rows:
        by_id = {r.resource_id: r for r in schedules}
        violation = max(
            0.0,
            *(
                float(
                    row.p_coefficient
                    * by_id[row.resource_id].active_power_mw[row.time_index]
                    + row.q_coefficient
                    * by_id[row.resource_id].reactive_power_mvar[row.time_index]
                    + (
                        row.previous_q_coefficient
                        * by_id[row.resource_id].reactive_power_mvar[row.time_index - 1]
                        if row.previous_q_coefficient
                        else 0.0
                    )
                    - row.upper
                )
                for row in reactive_rows
            ),
        )
        checks.append(
            _check(
                "REACTIVE_PLAN_ENVELOPE",
                "无功备用与计划过渡约束",
                violation <= request.policy.numerical_tolerance,
                actual=violation,
                limit=request.policy.numerical_tolerance,
                unit="Mvar",
            )
        )
    if name in case.joint_pq_plans:
        witness = PQPlanningWitness.model_validate(data.get("joint_pq_witness"))
        if witness.inputs != case.joint_pq_plans[name]:
            raise ValueError(
                "joint P/Q witness input differs from the requested window"
            )
        violation = pq_witness_violation(witness)
        checks.append(
            _check(
                "JOINT_PQ_PLAN",
                "实际初态至计划点的联合 P/Q 动态约束",
                violation <= request.policy.numerical_tolerance,
                actual=violation,
                limit=request.policy.numerical_tolerance,
                unit="MW/Mvar",
            )
        )
    evidence = RegionalExecution(
        region=name,
        solver_stop_reason=checked.stop_reason,
        optimization_quality=checked.optimization_quality,
        ac_recovery=checked.recovery,
        adoption_basis="ac_feasible_recovery"
        if feasible_recovery
        else "ac_optimized_recovery"
        if optimized_recovery
        else "standard",
        status=combined_status(
            [
                c.status
                for c in checks
                if not (feasible_recovery and c.code == "OPTIMIZATION_QUALITY")
            ]
        ),
        checks=tuple(checks),
        economic_cost_cny=float(cost) if checked.passed else None,
        reason="可行恢复计划；原经济目标最优性未证明"
        if feasible_recovery
        else ""
        if checked.passed
        else checked.stop_reason,
        time_minutes=_values(case.time_hours * 60),
        p_mw=_values(p),
        q_mvar=_values(q),
        p_target_mw=_values(targets["p_mw"] if targets is not None else p),
        q_target_mvar=_values(targets["q_mvar"] if targets is not None else q),
        storage_power_mw=_values(discharge - charge),
        storage_energy_mwh=_values(energy),
        storage_reachability=reachability,
        resource_p_mw={r.resource_id: _values(r.active_power_mw) for r in schedules},
        resource_q_mvar={
            r.resource_id: _values(r.reactive_power_mvar) for r in schedules
        },
    )
    return evidence, result if evidence.adoption_checks_passed else None


class ExecutionEngines:
    def __init__(
        self,
        case: ProjectCase,
        intraday: ProjectCase,
        plants: dict[str, PlantInputs],
        admm: ADMMResult,
        storage_enabled: bool,
        seconds: float,
        policy: ExecutionPolicy,
        admm_config: ADMMConfig | None = None,
        communication_config: CommunicationConfig | None = None,
        progress: Callable[[str], None] = lambda _: None,
        loss_calibration: Mapping[
            str, CoordinationNetworkCalibration | Mapping[str, np.ndarray]
        ]
        | None = None,
        rolling_progress: Callable[[RollingProgress], None] | None = None,
    ):
        self.case, self.intraday, self.plants = case, intraday, plants
        self.admm: ClusterReference = admm
        self.storage_enabled, self.seconds, self.policy = (
            storage_enabled,
            seconds,
            policy,
        )
        self.admm_config = admm_config or ADMMConfig()
        if policy.network_coordination is not None:
            self.admm_config = replace(
                self.admm_config, network_model="branch-flow-socp-v1"
            )
        if self.policy.cluster_power_factor is None:
            self.policy = self.policy.model_copy(
                update={
                    "cluster_power_factor": ClusterPowerFactorPolicy(
                        self.admm_config.aggregate_pf_target
                    )
                }
            )
        elif (
            self.policy.cluster_power_factor.target
            != self.admm_config.aggregate_pf_target
        ):
            raise ValueError("coordinator and final plan PF targets differ")
        self.communication_config = communication_config or CommunicationConfig()
        self.progress = progress
        self.rolling_progress = rolling_progress
        self.loss_calibration = loss_calibration
        self.day_ahead: tuple[RegionalExecution, ...] = ()
        self.rolling: RollingClusterEngine | None = None
        self.plan_pool: OwnedProcessPool | None = None
        self.day_ahead_recovery: CoordinationRecoveryReport | None = None

    def diagnose_windows(
        self,
        case: ProjectCase,
        admm: ClusterReference,
        number: int,
    ) -> tuple[RegionalCalibrationFeedback, ...]:
        if self.plan_pool is None:
            raise RuntimeError("regional diagnostic executor is not open")
        requests = [
            (self.plan_request(case, mg.name, admm), number) for mg in case.microgrids
        ]
        return tuple(self.plan_pool.map(diagnose_regional_reference, requests))

    def solve_windows(
        self,
        case: ProjectCase,
        admm: ClusterReference,
    ) -> dict[str, tuple[RegionalExecution, OptimizationResult | None]]:
        if self.plan_pool is None:
            raise RuntimeError("regional plan executor is not open")
        names = [mg.name for mg in case.microgrids]
        requests = [self.plan_request(case, name, admm) for name in names]
        results = self.plan_pool.map(realize_regional_plan, requests)
        return dict(zip(names, results, strict=True))

    def run_stage(self, stage: ExecutionStageName) -> tuple[RegionalExecution, ...]:
        if stage == "day_ahead":
            if self.policy.storage_reachability is not None:
                if self.policy.dynamic_tracking is None:
                    raise ValueError(
                        "storage reachability requires explicit response dynamics"
                    )
                self.case = with_storage_reachability(
                    self.case,
                    policy=self.policy.storage_reachability,
                    dynamics=self.policy.dynamic_tracking,
                    checkpoints={},
                    storage_enabled=self.storage_enabled,
                    execution_step_minutes=self.policy.device_step_minutes,
                )

            def coordinate(
                current: Mapping[str, CoordinationNetworkCalibration] | None,
                margin: tuple[float, ...],
                candidates: tuple[RegionalNetworkCandidate, ...] = (),
            ) -> ADMMResult:
                security = build_planning_security_trajectories(self.case)
                return run_admm_coordination(
                    replace(
                        self.case,
                        cluster_import_limit_mw=self.case.cluster_import_limit_mw
                        - cluster_import_tracking_margin(
                            region_count=len(self.case.microgrids),
                            tracking=self.policy.tracking_limits,
                        ),
                    )
                    if self.policy.cluster_safety is not None
                    else self.case,
                    storage_enabled=self.storage_enabled,
                    admm_config=replace(
                        self.admm_config, aggregate_pf_margin_mvar=margin
                    ),
                    communication_config=self.communication_config,
                    loss_calibration=current,
                    network_candidates={c.region: c for c in candidates}
                    if candidates
                    else None,
                    p_grid_security_floors_mw={
                        n: s.effective_floor_mw for n, s in security.items()
                    },
                )

            # Keep the existing serial day-ahead lifecycle; rolling windows own
            # their process pool. Both stages use the same recovery workflow.
            def realize(
                case: ProjectCase, reference: ClusterReference
            ) -> dict[str, tuple[RegionalExecution, OptimizationResult | None]]:
                rows: dict[
                    str, tuple[RegionalExecution, OptimizationResult | None]
                ] = {}
                for mg in case.microgrids:
                    try:
                        rows[mg.name] = realize_regional_plan(
                            self.plan_request(case, mg.name, reference)
                        )
                    except (ValueError, RuntimeError, FloatingPointError) as exc:
                        rows[mg.name] = (
                            RegionalExecution(
                                region=mg.name, status="unknown", reason=str(exc)
                            ),
                            None,
                        )
                return rows

            def diagnose(
                case: ProjectCase, reference: ClusterReference, number: int
            ) -> tuple[RegionalCalibrationFeedback, ...]:
                return tuple(
                    diagnose_regional_reference(
                        (self.plan_request(case, m.name, reference), number)
                    )
                    for m in case.microgrids
                )

            recovered = solve_with_reference_recovery(
                case=self.case,
                policy=self.policy,
                calibration=self.loss_calibration,
                coordinate=coordinate,
                coordinate_candidates=coordinate,
                aggregate_pf_target=self.admm_config.aggregate_pf_target,
                solve_regions=realize,
                diagnose_regions=diagnose,
                initial_reference=self.admm
                if isinstance(self.admm, ADMMResult)
                and self.policy.network_coordination is None
                and self.policy.cluster_safety is None
                and self.admm.network_model == self.admm_config.network_model
                else None,
                storage_enabled=self.storage_enabled,
                stage="day_ahead",
            )
            self.day_ahead_recovery = recovered.report
            payload = recovered.payload
            if recovered.report.accepted and payload is not None:
                self.admm = payload.reference
                self.day_ahead = tuple(r for r, _ in payload.plans.values())
            else:
                rows = {} if payload is None else payload.plans
                self.day_ahead = tuple(
                    (
                        rows[m.name][0]
                        if m.name in rows
                        else RegionalExecution(region=m.name, status="unknown")
                    ).model_copy(
                        update={
                            "status": "violated"
                            if m.name in rows and rows[m.name][0].status == "violated"
                            else "unknown",
                            "reason": (rows[m.name][0].reason if m.name in rows else "")
                            + f"；日前协调计划未获采用：{recovered.report.stop_reason}；{recovered.report.reason}",
                            # Candidate costs stay in the round journal; they are
                            # not the realized cost of an unadopted joint plan.
                            "economic_cost_cny": None,
                            "checks": (
                                *(rows[m.name][0].checks if m.name in rows else ()),
                                ExecutionCheck(
                                    code="REFERENCE_RECOVERY",
                                    label="跨层计划采用",
                                    status="unknown",
                                ),
                            ),
                        }
                    )
                    for m in self.case.microgrids
                )
            return self.day_ahead
        if stage == "intraday":
            self.rolling = RollingClusterEngine(
                case=self.intraday,
                original_case=self.case,
                plants=self.plants,
                original_admm=self.admm,
                day_ahead=self.day_ahead,
                storage_enabled=self.storage_enabled,
                policy=self.policy,
                admm_config=self.admm_config,
                communication_config=self.communication_config,
                loss_calibration=self.loss_calibration,
                solve_regions=self.solve_windows,
                diagnose_regions=self.diagnose_windows,
                progress=self.progress,
                rolling_progress=self.rolling_progress,
            )
            with OwnedProcessPool(self.policy.regional_plan_workers) as pool:
                self.plan_pool = pool
                try:
                    self.rolling.run()
                finally:
                    self.plan_pool = None
            return self.rolling.intraday_evidence()
        evidence: list[RegionalExecution] = []
        for mg in self.intraday.microgrids:
            try:
                result = self.minute(mg.name)
            except (ValueError, RuntimeError, FloatingPointError) as exc:
                result = RegionalExecution(
                    region=mg.name, status="unknown", reason=str(exc)
                )
            evidence.append(result)
        return tuple(evidence)

    def plan_request(
        self, case: ProjectCase, name: str, reference: ClusterReference
    ) -> RegionalPlanRequest:
        return RegionalPlanRequest(
            case,
            name,
            np.array(reference.p_references_mw[name], dtype=float, copy=True),
            np.array(reference.q_references_mvar[name], dtype=float, copy=True),
            self.storage_enabled,
            self.seconds,
            self.policy,
        )

    def minute(self, name: str) -> RegionalExecution:
        if self.rolling is not None and self.rolling.safety is not None:
            return self.rolling.safety.minute_evidence(name, self.rolling.total)
        return RegionalExecution(
            region=name,
            status="unknown" if self.rolling is not None else "not_computed",
            reason=self.rolling.safety_failure
            if self.rolling is not None
            else "Rolling session was not started; no execution evidence is available",
        )


def run_cluster_execution(
    case: ProjectCase,
    admm: ADMMResult,
    *,
    storage_enabled: bool,
    time_limit_seconds: float,
    packaged_inputs: bool,
    events: list[ScenarioEvent],
    progress: Callable[[str], None],
    rolling_progress: Callable[[RollingProgress], None] | None = None,
    policy: ExecutionPolicy | None = None,
    admm_config: ADMMConfig | None = None,
    communication_config: CommunicationConfig | None = None,
    loss_calibration: Mapping[
        str, CoordinationNetworkCalibration | Mapping[str, np.ndarray]
    ]
    | None = None,
) -> ClusterExecution:
    policy = policy or ExecutionPolicy(
        regional_plan_workers=3,
        numerical_threads_per_worker=1,
        coordination_model_reuse=True,
        storage_reserve=StorageReservePolicy(),
        reactive_planning=ReactivePlanningPolicy(),
        coordination_recovery=CoordinationRecoveryPolicy(),
        network_candidates=NetworkCandidatePolicy(),
        network_coordination=NetworkCoordinationPolicy(),
        storage_reachability=StorageReachabilityPolicy(),
    )
    policy = policy.model_copy(
        update={
            "cluster_power_factor": policy.cluster_power_factor
            or ClusterPowerFactorPolicy(
                (admm_config or ADMMConfig()).aggregate_pf_target
            ),
            "computation_quality": admm_config.quality_policy if admm_config else None,
            "reserve_delivery": (policy.reserve_delivery or ReserveDeliveryPolicy())
            if policy.storage_reserve is not None and storage_enabled
            else None,
            "active_tracking_version": "cluster-command-projection-v1",
            "cluster_safety": policy.cluster_safety or ClusterSafetyPolicy(),
            "ac_recovery": policy.ac_recovery or ACRecoveryPolicy(),
            "device_capability_version": "cluster-device-v1",
            "reactive_tracking_version": "cluster-command-projection-v1",
            "reactive_dynamics_version": "cluster-device-v1",
            "numerical_threads_per_worker": 1
            if policy.regional_plan_workers > 1
            else None,
        }
    )
    if policy.dynamic_tracking is None:
        policy = ExecutionPolicy.model_validate(
            {
                **policy.model_dump(),
                "dynamic_tracking": DynamicTrackingPolicy().model_dump(),
            }
        )
    intraday, plants, basis = _execution_inputs(case, packaged_inputs, events)
    engines = ExecutionEngines(
        case,
        intraday,
        plants,
        admm,
        storage_enabled,
        time_limit_seconds,
        policy,
        admm_config,
        communication_config,
        progress,
        loss_calibration,
        rolling_progress,
    )
    result = verify_execution(
        names=tuple(m.name for m in case.microgrids),
        limit_mw=case.cluster_import_limit_mw,
        converged=admm.converged,
        storage_enabled=storage_enabled,
        policy=policy,
        input_basis=basis,
        dataset_sha256=case.dataset_sha256,
        run_stage=engines.run_stage,
        progress=progress,
    )
    return result.model_copy(
        update={
            "algorithm_version": "cluster-rolling-v6",
            "rolling_updates": engines.rolling.updates if engines.rolling else (),
            "safety": engines.rolling.safety_evidence() if engines.rolling else None,
            "day_ahead_recovery": engines.day_ahead_recovery,
            "policy": engines.rolling.policy if engines.rolling else policy,
            "reserve_recovery_history": tuple(engines.rolling.reserve_recovery_history)
            if engines.rolling
            else (),
            "reserve_observations": tuple(engines.rolling.reserve_observations)
            if engines.rolling
            else (),
        }
    )

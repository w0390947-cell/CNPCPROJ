"""Bridge existing numerical engines to the automatic execution workflow.

No centralized schedule is substituted for an ADMM realization. Each stage
retains its own input, target, output and failure evidence.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.analysis import validate_result
from oilfield_energy.data import ProjectCase
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.hierarchy_types import (
    ADMMConfig,
    ADMMResult,
    CommunicationConfig,
    DeviceTrackingResult,
)
from oilfield_energy.model import OptimizationResult
from oilfield_energy.modules.control.api import assess_dynamic_tracking
from oilfield_energy.modules.control.contracts import (
    BusSeries,
    DynamicTrackingAssessment,
    DynamicTrackingPolicy,
    PlantInputs,
)
from oilfield_energy.modules.dispatch.contracts import (
    ReactivePlanningPolicy,
    StorageReservePolicy,
)
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
    ExecutionCheck,
    ExecutionPolicy,
    ExecutionStageName,
    RegionalExecution,
    RollingProgress,
)

from .cluster_rolling import RollingClusterEngine, execution_time_scale
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


@dataclass(frozen=True)
class RegionalPlanRequest:
    """Owned regional inputs; contains no live engine or solver object."""

    case: ProjectCase
    name: str
    p_target_mw: NDArray[np.float64]
    q_target_mvar: NDArray[np.float64]
    storage_enabled: bool
    seconds: float
    policy: ExecutionPolicy


def realize_regional_plan(
    request: RegionalPlanRequest,
) -> tuple[RegionalExecution, OptimizationResult | None]:
    """Solve and certify one region without mutating orchestration state."""
    case, name = request.case, request.name
    targets = {"p_mw": request.p_target_mw, "q_mvar": request.q_target_mvar}
    security = build_planning_security_trajectories(case, [name])
    checked = solve_case_ac_consistent(
        case,
        [name],
        storage_enabled=request.storage_enabled,
        cluster_coordination=False,
        pcc_targets={name: targets},
        tracking_limits=request.policy.tracking_limits,
        time_limit_seconds=request.seconds,
        quality_policy=request.policy.computation_quality,
        p_grid_security_floors_mw={name: security[name].effective_floor_mw},
    )
    if not checked.optimization.success:
        infeasible = checked.stop_reason == "solver_infeasible"
        reason = (
            "当前设备、电网和逐时段 P/Q 偏差约束下，区域目标不可行"
            if infeasible
            else "区域求解未获得可用计划（不代表已证明目标不可行）"
        )
        reason += f"：{checked.stop_reason}；{checked.optimization.message}"
        evidence = RegionalExecution(
            region=name,
            optimization_quality=checked.optimization_quality,
            status="violated" if infeasible else "unknown",
            reason=reason,
            checks=(
                ExecutionCheck(
                    code="DEVICE_PLAN",
                    label="设备与运行约束（含逐时段目标偏差）",
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
        *_tracking(p, q, targets["p_mw"], targets["q_mvar"], request.policy),
    ]
    if checked.optimization_quality:
        quality = checked.optimization_quality[-1]
        checks.append(ExecutionCheck(
            code="OPTIMIZATION_QUALITY", label="优化最优性精度",
            status="passed" if quality.status == "satisfied" else "unknown",
            actual=quality.attempts[quality.selected_attempt - 1].relative_gap,
            limit=quality.target_relative_gap,
            reason=quality.status,
        ))
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
    evidence = RegionalExecution(
        region=name,
        optimization_quality=checked.optimization_quality,
        status=combined_status([c.status for c in checks]),
        checks=tuple(checks),
        economic_cost_cny=float(cost) if checked.passed else None,
        reason="" if checked.passed else checked.stop_reason,
        time_minutes=_values(case.time_hours * 60),
        p_mw=_values(p),
        q_mvar=_values(q),
        p_target_mw=_values(targets["p_mw"]),
        q_target_mvar=_values(targets["q_mvar"]),
        storage_power_mw=_values(discharge - charge),
        storage_energy_mwh=_values(energy),
        resource_p_mw={r.resource_id: _values(r.active_power_mw) for r in schedules},
        resource_q_mvar={
            r.resource_id: _values(r.reactive_power_mvar) for r in schedules
        },
    )
    return evidence, result if all(c.status == "passed" for c in checks) else None


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
        loss_calibration: Mapping[str, Mapping[str, np.ndarray]] | None = None,
        rolling_progress: Callable[[RollingProgress], None] | None = None,
    ):
        self.case, self.intraday, self.plants = case, intraday, plants
        self.admm, self.storage_enabled, self.seconds, self.policy = (
            admm,
            storage_enabled,
            seconds,
            policy,
        )
        self.intraday_plans: dict[str, OptimizationResult] = {}
        self.admm_config = admm_config or ADMMConfig()
        self.communication_config = communication_config or CommunicationConfig()
        self.progress = progress
        self.rolling_progress = rolling_progress
        self.loss_calibration = loss_calibration
        self.day_ahead: tuple[RegionalExecution, ...] = ()
        self.rolling: RollingClusterEngine | None = None
        self.plan_pool: OwnedProcessPool | None = None

    def solve_windows(
        self,
        case: ProjectCase,
        admm: ADMMResult,
    ) -> dict[str, tuple[RegionalExecution, OptimizationResult | None]]:
        if self.plan_pool is None:
            raise RuntimeError("regional plan executor is not open")
        names = [mg.name for mg in case.microgrids]
        requests = [self.plan_request(case, name, admm) for name in names]
        results = self.plan_pool.map(realize_regional_plan, requests)
        return dict(zip(names, results, strict=True))

    def run_stage(self, stage: ExecutionStageName) -> tuple[RegionalExecution, ...]:
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
        case = self.case if stage == "day_ahead" else self.intraday
        evidence: list[RegionalExecution] = []
        for index, mg in enumerate(case.microgrids):
            try:
                if stage == "minute":
                    result = self.minute(mg.name, index)
                else:
                    result = self.plan(case, mg.name, stage)
            except (ValueError, RuntimeError, FloatingPointError) as exc:
                result = RegionalExecution(
                    region=mg.name, status="unknown", reason=str(exc)
                )
            evidence.append(result)
        if stage == "day_ahead":
            self.day_ahead = tuple(evidence)
        return tuple(evidence)

    def plan_request(
        self, case: ProjectCase, name: str, reference: ADMMResult
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

    def plan(
        self,
        case: ProjectCase,
        name: str,
        stage: ExecutionStageName,
        *,
        admm: ADMMResult | None = None,
    ) -> RegionalExecution:
        evidence, result = realize_regional_plan(
            self.plan_request(case, name, admm or self.admm)
        )
        if stage == "intraday" and result is not None:
            self.intraday_plans[name] = result
        return evidence

    def minute(self, name: str, index: int) -> RegionalExecution:
        if self.rolling is not None:
            result = self.rolling.results.get(name)
            if result is None:
                attempted = bool(self.rolling.accepted[name])
                return RegionalExecution(
                    region=name,
                    status="unknown" if attempted else "not_computed",
                    reason="滚动执行提前停止，未获得完整分钟校核证据；已完成窗口及电量保留在滚动记录中",
                )
            evidence = self.minute_evidence(name, result)
            if len(result.time_minutes) != self.rolling.total:
                reason = self.rolling.updates[-1].reason
                checks = (
                    *evidence.checks,
                    ExecutionCheck(
                        code="ROLLING_COMPLETE",
                        label="完整滚动执行",
                        status="unknown",
                        reason=reason,
                    ),
                )
                return evidence.model_copy(
                    update={
                        "status": combined_status([c.status for c in checks]),
                        "reason": reason,
                        "checks": checks,
                    }
                )
            return evidence
        if name not in self.intraday_plans:
            return RegionalExecution(
                region=name,
                status="not_computed",
                reason="日内计划未通过独立 AC 校核，未采用为分钟执行指令",
            )
        mg = next(m for m in self.intraday.microgrids if m.name == name)
        result = simulate_device_tracking(
            self.intraday,
            mg,
            self.intraday_plans[name],
            plant_inputs=self.plants[name],
            storage_enabled=self.storage_enabled,
            seed=self.policy.random_seed + index,
            config=execution_time_scale(self.policy),
        )
        return self.minute_evidence(name, result)

    def minute_evidence(
        self, name: str, result: DeviceTrackingResult
    ) -> RegionalExecution:
        mg = next(m for m in self.intraday.microgrids if m.name == name)
        energy = result.storage_energy_mwh
        tol = self.policy.numerical_tolerance
        previous_energy = np.r_[mg.storage.e_initial_mwh, energy[:-1]]
        balance = (
            previous_energy
            - np.maximum(result.storage_actual_mw, 0) / mg.storage.eta_discharge / 60
            + np.maximum(-result.storage_actual_mw, 0) * mg.storage.eta_charge / 60
        )
        dynamic: dict[Literal["p", "q"], DynamicTrackingAssessment] = {}
        tracking_checks: list[ExecutionCheck] = _tracking(
            result.pcc_actual_mw,
            result.qcc_actual_mvar,
            result.pcc_command_mw,
            result.qcc_command_mvar,
            self.policy,
        )
        if self.policy.dynamic_tracking is not None:
            dynamic = {
                "p": assess_dynamic_tracking(
                    time_minutes=_values(result.time_minutes),
                    target=_values(result.pcc_command_mw),
                    actual=_values(result.pcc_actual_mw),
                    step_minutes=self.policy.device_step_minutes,
                    unit="MW",
                    limit=self.policy.p_tracking_tolerance_mw,
                    numerical_tolerance=tol,
                    policy=self.policy.dynamic_tracking,
                ),
                "q": assess_dynamic_tracking(
                    time_minutes=_values(result.time_minutes),
                    target=_values(result.qcc_command_mvar),
                    actual=_values(result.qcc_actual_mvar),
                    step_minutes=self.policy.device_step_minutes,
                    unit="Mvar",
                    limit=self.policy.q_tracking_tolerance_mvar,
                    numerical_tolerance=tol,
                    policy=self.policy.dynamic_tracking,
                ),
            }
            tracking_checks = []
            for axis, assessment in dynamic.items():
                label = "有功" if axis == "p" else "无功"
                raw = assessment.raw_max_error
                raw_text = (
                    "无有效数据" if raw is None else f"{raw:.6g} {assessment.unit}"
                )
                reason = (
                    f"全程最大偏差 {raw_text}（含暂态），发生在第 "
                    f"{assessment.raw_max_error_time_minute} 分钟；"
                    f"连续超限最长 {assessment.longest_outside_band_minutes:g} 分钟。"
                )
                reasons = list(
                    dict.fromkeys(
                        t.reason
                        for t in assessment.transitions
                        if t.response_status != "passed" or t.steady_status != "passed"
                    )
                )
                tracking_checks.extend(
                    [
                        ExecutionCheck(
                            code=f"{axis.upper()}_RESPONSE",
                            label=f"{label}动态响应期限",
                            status=assessment.response_status,
                            reason=reason + "；".join(reasons),
                        ),
                        ExecutionCheck(
                            code=f"{axis.upper()}_TRACKING",
                            label=f"{label}响应期限后最大偏差",
                            status=assessment.steady_status,
                            actual=assessment.post_deadline_max_error,
                            limit=assessment.limit,
                            unit=assessment.unit,
                            reason=f"期限后至少连续观察 {self.policy.dynamic_tracking.confirmation_samples} 点；"
                            + "；".join(reasons),
                        ),
                    ]
                )
        checks = [
            ExecutionCheck(
                code="AC_NETWORK",
                label="分钟级独立网络校核",
                status="unknown"
                if result.network_invalid_steps
                else "passed"
                if result.network_security_passed
                else "violated",
                actual=float(result.network_violation_steps),
                limit=0,
            ),
            _check(
                "STORAGE_ENERGY",
                "储能能量边界",
                bool(
                    (
                        (energy >= mg.storage.e_min_mwh - tol)
                        & (energy <= mg.storage.e_max_mwh + tol)
                    ).all()
                ),
            ),
            _check(
                "STORAGE_BALANCE",
                "储能能量守恒",
                bool(np.abs(energy - balance).max() <= tol),
            ),
            _check(
                "STORAGE_RAMP",
                "储能响应与限幅",
                bool(
                    len(result.storage_dynamics) == len(result.time_minutes)
                    and all(
                        r.ramp_compliant or r.physical_override or r.hard_override
                        for r in result.storage_dynamics
                    )
                ),
            ),
            _check(
                "REACTIVE_EXECUTION",
                "无功执行能力与响应",
                bool(
                    result.reactive_execution_known.all()
                    and result.reactive_dispatch_unserved_mvar.max() <= tol
                ),
            ),
            _check(
                "LOCAL_PROTECTION",
                "防倒送与功率因数",
                bool(
                    result.no_reverse_violations_after_safety == 0
                    and result.pf_violations_after_safety == 0
                ),
            ),
            *tracking_checks,
        ]
        if not self.storage_enabled:
            checks.append(
                _check(
                    "STORAGE_DISABLED",
                    "储能停用约束",
                    bool(
                        np.abs(result.storage_actual_mw).max() <= tol
                        and np.abs(result.storage_reactive_actual_mvar).max() <= tol
                        and np.abs(energy - mg.storage.e_initial_mwh).max() <= tol
                    ),
                )
            )
        # Execution identities come from the plant snapshot, not the last
        # attempted plan (which may have failed and never been adopted).
        active = {
            resource_id: _values(values)
            for resource_id, values in result.wind_resource_actual_mw.items()
        }
        active.update(
            {
                mg.resource_id("pv", bus): _values(values)
                for bus, values in result.pv_station_actual_mw.items()
            }
        )
        active[mg.resource_id("storage", mg.storage.bus)] = _values(
            result.storage_actual_mw
        )
        active[mg.resource_id("svg", mg.svg_bus)] = _values(
            np.zeros(len(result.time_minutes))
        )
        if set(active) != set(result.reactive_resource_actual_mvar):
            raise ValueError("executed active/reactive resource identities differ")
        return RegionalExecution(
            region=name,
            status=combined_status([c.status for c in checks]),
            checks=tuple(checks),
            time_minutes=_values(result.time_minutes),
            p_mw=_values(result.pcc_actual_mw),
            q_mvar=_values(result.qcc_actual_mvar),
            p_target_mw=_values(result.pcc_command_mw),
            q_target_mvar=_values(result.qcc_command_mvar),
            storage_power_mw=_values(result.storage_actual_mw),
            storage_energy_mwh=_values(energy),
            resource_p_mw=active,
            dynamic_tracking=dynamic,
            resource_q_mvar={
                key: _values(values)
                for key, values in result.reactive_resource_actual_mvar.items()
            },
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
    loss_calibration: Mapping[str, Mapping[str, np.ndarray]] | None = None,
) -> ClusterExecution:
    policy = policy or ExecutionPolicy(
        regional_plan_workers=3,
        numerical_threads_per_worker=1,
        coordination_model_reuse=True,
        storage_reserve=StorageReservePolicy(),
        reactive_planning=ReactivePlanningPolicy(),
    )
    policy = policy.model_copy(
        update={
            "computation_quality": admm_config.quality_policy if admm_config else None,
            "active_tracking_version": "active-tracking-allocation-v1",
            "reactive_tracking_version": "reactive-tracking-v1",
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
            "rolling_updates": engines.rolling.updates if engines.rolling else (),
            "policy": engines.rolling.policy if engines.rolling else policy,
            "minute_reserves": tuple(engines.rolling.minute_reserves)
            if engines.rolling
            else (),
            "reserve_target_adjustments": tuple(
                engines.rolling.reserve_target_adjustments
            )
            if engines.rolling
            else (),
        }
    )

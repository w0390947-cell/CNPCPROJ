"""Numerical bridge for causal, synchronized cluster rolling execution."""

from collections.abc import Callable, Generator, Mapping
from dataclasses import replace
from math import gcd
from typing import cast

import numpy as np
from numpy.typing import NDArray

from oilfield_energy.admm import CoordinationWorkspace, run_admm_coordination
from oilfield_energy.data import ProjectCase
from oilfield_energy.device_control import device_tracking_session
from oilfield_energy.hierarchy_types import (
    ADMMConfig,
    ADMMResult,
    CommunicationConfig,
    DeviceTrackingResult,
    TimeScaleConfig,
)
from oilfield_energy.model import OptimizationResult
from oilfield_energy.modules.control.contracts import (
    DeviceCheckpoint,
    DevicePlanUpdate,
    PlantInputs,
    StopDeviceSession,
)
from oilfield_energy.planning_security import build_planning_security_trajectories
from oilfield_energy.resource_control_contracts import ResourceSchedule
from oilfield_energy.workflows.cluster_execution.api import (
    combined_status,
    executable_regional_plan,
    observe_storage_reserve,
    run_feedback_loop,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionCheck,
    ExecutionPolicy,
    MinuteReserveObservation,
    RegionalExecution,
    ReserveTargetAdjustment,
    RollingProgress,
    RollingUpdate,
)

from .cluster_reactive import with_reactive_planning
from .cluster_reserve import with_storage_reserves


def execution_time_scale(policy: ExecutionPolicy) -> TimeScaleConfig:
    """Use the recorded response parameters in both the plant and its assessment."""
    dynamic = policy.dynamic_tracking
    if dynamic is None:
        return TimeScaleConfig(device_step_minutes=policy.device_step_minutes)
    return TimeScaleConfig(
        device_step_minutes=policy.device_step_minutes,
        device_time_constant_minutes=dynamic.time_constant_minutes,
        pv_device_time_constant_minutes=dynamic.pv_time_constant_minutes,
        active_power_ramp_mw_per_minute=dynamic.p_ramp_mw_per_minute,
        reactive_power_ramp_mvar_per_minute=dynamic.q_ramp_mvar_per_minute,
    )


def copy_series(values: np.ndarray) -> NDArray[np.float64]:
    """Own a float trajectory at the legacy numerical adapter boundary."""
    return np.array(values, dtype=np.float64, copy=True)


def forecast_grid(case: ProjectCase, step: int) -> ProjectCase:
    """Piecewise-held forecast resampling preserves original interval energy."""
    total = len(case.time_hours) * case.assumptions.dt_hours * 60
    if not float(total).is_integer() or int(total) % step:
        raise ValueError("forecast duration must align with rolling intervals")
    minutes = np.arange(0, int(total), step)
    indices = np.floor(minutes / (case.assumptions.dt_hours * 60)).astype(int)
    return replace(
        case,
        time_hours=minutes / 60,
        price_cny_per_mwh=copy_series(case.price_cny_per_mwh[indices]),
        assumptions=replace(case.assumptions, dt_hours=step / 60),
        microgrids=[
            replace(
                mg,
                **{
                    key: {
                        bus: values[indices].copy()
                        for bus, values in getattr(mg, key).items()
                    }
                    for key in (
                        "load_p_mw",
                        "load_q_mvar",
                        "wind_available_mw",
                        "pv_available_mw",
                    )
                },
            )
            for mg in case.microgrids
        ],
    )


def window_case(
    case: ProjectCase,
    start: int,
    stop: int,
    energies: dict[str, float],
    terminals: dict[str, float],
) -> ProjectCase:
    return replace(
        case,
        time_hours=copy_series(case.time_hours[start:stop]),
        price_cny_per_mwh=copy_series(case.price_cny_per_mwh[start:stop]),
        microgrids=[
            replace(
                mg,
                storage=replace(
                    mg.storage,
                    e_initial_mwh=energies[mg.name],
                    e_terminal_mwh=terminals[mg.name],
                ),
                **{
                    key: {
                        bus: values[start:stop].copy()
                        for bus, values in getattr(mg, key).items()
                    }
                    for key in (
                        "load_p_mw",
                        "load_q_mvar",
                        "wind_available_mw",
                        "pv_available_mw",
                    )
                },
            )
            for mg in case.microgrids
        ],
    )


def initial_plan(
    result: OptimizationResult, name: str, length: int
) -> OptimizationResult:
    """Seed device identities/state; only explicitly adopted segments may execute."""
    data = result.microgrids[name]
    schedules = data["resource_schedules"]
    p, q = data["p_grid_mw"], data["q_grid_mvar"]
    if not isinstance(p, np.ndarray) or not isinstance(q, np.ndarray):
        raise ValueError("invalid PCC schedule")
    if not isinstance(schedules, (tuple, list)):
        raise ValueError("invalid resource schedules")
    raw = cast(tuple[object, ...] | list[object], schedules)
    if not all(isinstance(r, ResourceSchedule) for r in raw):
        raise ValueError("invalid resource schedules")
    return replace(
        result,
        microgrids={
            name: {
                "p_grid_mw": np.full(length, p[0]),
                "q_grid_mvar": np.full(length, q[0]),
                "resource_schedules": tuple(
                    replace(
                        r,
                        active_power_mw=np.full(length, r.active_power_mw[0]),
                        reactive_power_mvar=np.full(length, r.reactive_power_mvar[0]),
                    )
                    for r in raw
                    if isinstance(r, ResourceSchedule)
                ),
            }
        },
    )


def first_command(
    evidence: RegionalExecution, start: int, count: int
) -> DevicePlanUpdate:
    return DevicePlanUpdate(
        start_minute=start,
        p_mw=(evidence.p_mw[0],) * count,
        q_mvar=(evidence.q_mvar[0],) * count,
        resource_p_mw={r: (v[0],) * count for r, v in evidence.resource_p_mw.items()},
        resource_q_mvar={
            r: (v[0],) * count for r, v in evidence.resource_q_mvar.items()
        },
    )


class RollingClusterEngine:
    def __init__(
        self,
        *,
        case: ProjectCase,
        original_case: ProjectCase,
        plants: dict[str, PlantInputs],
        original_admm: ADMMResult,
        day_ahead: tuple[RegionalExecution, ...],
        storage_enabled: bool,
        policy: ExecutionPolicy,
        admm_config: ADMMConfig,
        communication_config: CommunicationConfig,
        loss_calibration: Mapping[str, Mapping[str, np.ndarray]] | None,
        solve_regions: Callable[
            [ProjectCase, ADMMResult],
            dict[str, tuple[RegionalExecution, OptimizationResult | None]],
        ],
        progress: Callable[[str], None],
        rolling_progress: Callable[[RollingProgress], None] | None = None,
    ):
        original_step = original_case.assumptions.dt_hours * 60
        if not float(original_step).is_integer():
            raise ValueError("rolling execution requires integer minute inputs")
        # Quarter-hour defaults also support coarser 24-point forecasts without
        # using the minute plant as a forecast source.
        self.step = gcd(int(original_step), policy.update_minutes)
        self.policy = policy.model_copy(update={"update_minutes": self.step})
        self.case = forecast_grid(case, self.step)
        self.original_case, self.original_admm = original_case, original_admm
        self.plants, self.storage_enabled = plants, storage_enabled
        self.admm_config, self.communication_config = admm_config, communication_config
        self.loss_calibration = loss_calibration
        self.solve_regions, self.progress = solve_regions, progress
        self.rolling_progress = rolling_progress
        self.total = len(self.case.time_hours) * self.step
        self.names = tuple(m.name for m in self.case.microgrids)
        self.day_ahead = {r.region: r for r in day_ahead}
        self.energies = {m.name: m.storage.e_initial_mwh for m in self.case.microgrids}
        self.sessions: dict[
            str,
            Generator[DeviceCheckpoint, DevicePlanUpdate | None, DeviceTrackingResult],
        ] = {}
        self.checkpoints: dict[str, DeviceCheckpoint] = {}
        self.results: dict[str, DeviceTrackingResult] = {}
        self.commands: dict[str, DevicePlanUpdate] = {}
        self.accepted: dict[str, list[RegionalExecution]] = {n: [] for n in self.names}
        self.updates: tuple[RollingUpdate, ...] = ()
        self.last_reference: ADMMResult | None = None
        self.last_start = 0
        self.last_step = self.step
        self.accepted_times: list[float] = []
        self.minute_reserves: list[MinuteReserveObservation] = []
        self.reserve_target_adjustments: list[ReserveTargetAdjustment] = []
        self.last_reserve_replan = -self.total
        self.current_terminal: dict[str, float] = {}
        self.minute_forecast = forecast_grid(case, 1)
        self.coordination_workspace = (
            CoordinationWorkspace() if policy.coordination_model_reuse else None
        )

    def prepare(
        self, start: int, end: int, horizon_end: int, *, minute_replan: bool = False
    ) -> RollingUpdate:
        terminals: dict[str, float] = {}
        for mg in self.case.microgrids:
            ahead = self.day_ahead.get(mg.name)
            if (
                ahead is None
                or not executable_regional_plan(ahead)
                or not ahead.storage_energy_mwh
            ):
                reason = ahead.reason if ahead is not None else "缺少日前区域计划"
                if ahead is not None and not reason:
                    reason = "; ".join(
                        c.label for c in ahead.checks if c.status != "passed"
                    )
                raise ValueError(
                    f"{mg.name} 无合格日前电量轨迹，无法确定滚动末端目标：{reason}"
                )
            energy_times = (
                np.arange(len(ahead.storage_energy_mwh))
                * self.original_case.assumptions.dt_hours
                * 60
            )
            terminals[mg.name] = float(
                np.interp(horizon_end, energy_times, ahead.storage_energy_mwh)
            )
            tolerance = self.policy.numerical_tolerance
            if (
                not mg.storage.e_min_mwh - tolerance
                <= terminals[mg.name]
                <= mg.storage.e_max_mwh + tolerance
            ):
                raise ValueError(
                    f"{mg.name} day-ahead terminal energy exceeds physical bounds"
                )
            terminals[mg.name] = min(
                mg.storage.e_max_mwh, max(mg.storage.e_min_mwh, terminals[mg.name])
            )
            if not self.storage_enabled:
                terminals[mg.name] = self.energies[mg.name]
        if minute_replan:
            terminals = dict(self.current_terminal)
        original_index = min(
            int(start / (self.original_case.assumptions.dt_hours * 60)),
            len(self.original_case.time_hours) - 1,
        )
        previous_p = {
            n: float(self.original_admm.p_references_mw[n][original_index])
            for n in self.names
        }
        previous_q = {
            n: float(self.original_admm.q_references_mvar[n][original_index])
            for n in self.names
        }
        if self.updates:
            raise RuntimeError("a rolling engine cannot be reused")
        # Previous target means the target issued by the immediately preceding
        # window for this time, not a hindsight replacement of past commands.
        previous_source = "day_ahead_admm"
        if self.last_reference is not None and (
            start - self.last_start
        ) // self.last_step < len(self.last_reference.p_references_mw[self.names[0]]):
            previous_source = "preceding_rolling_admm"
            offset = (start - self.last_start) // self.last_step
            previous_p = {
                n: float(self.last_reference.p_references_mw[n][offset])
                for n in self.names
            }
            previous_q = {
                n: float(self.last_reference.q_references_mvar[n][offset])
                for n in self.names
            }
        evidence = RollingUpdate(
            start_minute=start,
            end_minute=end,
            horizon_end_minute=horizon_end,
            status="unknown",
            initial_energy_mwh=dict(self.energies),
            initial_reactive_mvar={
                n: dict(s.reactive_power_mvar) for n, s in self.checkpoints.items()
            },
            terminal_target_mwh=terminals,
            unreserved_terminal_target_mwh=dict(terminals),
            previous_p_target_mw=previous_p,
            previous_q_target_mvar=previous_q,
            previous_target_source=previous_source,
            forecast_basis=(
                "同版本日内预测按窗口读取；反馈仅使用已执行状态，不读取未来分钟实绩"
                if self.case.profile_kind == "intraday"
                else "捕获算例原预测按窗口读取；仅反馈已执行状态，无独立日内预测版本"
            ),
        )
        try:
            planning_step = 1 if minute_replan else self.step
            window = window_case(
                self.minute_forecast if minute_replan else self.case,
                start // planning_step,
                horizon_end // planning_step,
                self.energies,
                terminals,
            )
            if self.storage_enabled and self.policy.storage_reserve is not None:
                window = with_storage_reserves(
                    window, self.policy.storage_reserve, self.total - start
                )
                evidence = evidence.model_copy(
                    update={
                        "terminal_target_mwh": {
                            m.name: m.storage.terminal_energy_mwh
                            for m in window.microgrids
                        },
                        "reserve_up_mw": {
                            n: r.up_mw for n, r in window.storage_reserves.items()
                        },
                        "reserve_down_mw": {
                            n: r.down_mw for n, r in window.storage_reserves.items()
                        },
                        "reserve_energy_floor_mwh": {
                            n: r.energy_floor_mwh
                            for n, r in window.storage_reserves.items()
                        },
                        "reserve_energy_ceiling_mwh": {
                            n: r.energy_ceiling_mwh
                            for n, r in window.storage_reserves.items()
                        },
                    }
                )
            if self.policy.reactive_planning is not None:
                if self.policy.dynamic_tracking is None:
                    raise ValueError(
                        "reactive planning requires explicit response dynamics"
                    )
                window = with_reactive_planning(
                    window,
                    policy=self.policy.reactive_planning,
                    dynamics=self.policy.dynamic_tracking,
                    checkpoints=self.checkpoints,
                    storage_enabled=self.storage_enabled,
                )
            security = build_planning_security_trajectories(window, list(self.names))
            indices = np.floor(
                window.time_hours / self.original_case.assumptions.dt_hours
            ).astype(int)
            losses = (
                None
                if self.loss_calibration is None
                else {
                    name: {
                        key: copy_series(values[indices]) for key, values in row.items()
                    }
                    for name, row in self.loss_calibration.items()
                }
            )
            reference = run_admm_coordination(
                window,
                self.names,
                admm_config=self.admm_config,
                communication_config=self.communication_config,
                p_grid_security_floors_mw={
                    n: security[n].effective_floor_mw for n in self.names
                },
                storage_enabled=self.storage_enabled,
                loss_calibration=losses,
                workspace=self.coordination_workspace,
            )
            evidence = evidence.model_copy(
                update={
                    "admm_iterations": len(reference.history),
                    "coordination_quality": reference.quality_budgets,
                    "p_target_mw": {
                        n: tuple(map(float, reference.p_references_mw[n]))
                        for n in self.names
                    },
                    "q_target_mvar": {
                        n: tuple(map(float, reference.q_references_mvar[n]))
                        for n in self.names
                    },
                }
            )
            if not reference.converged:
                return evidence.model_copy(
                    update={"reason": "滚动 ADMM 未收敛，停止采用后续目标"}
                )
            plans = self.solve_regions(window, reference)
            if set(plans) != set(self.names):
                raise ValueError(
                    "regional plans must cover exactly the expected regions"
                )
            evidence = evidence.model_copy(
                update={
                    "regional_checks": {n: r.checks for n, (r, _) in plans.items()},
                    "optimization_quality": {n: r.optimization_quality for n, (r, _) in plans.items()},
                }
            )
            for name, (regional, plan) in plans.items():
                if plan is None or not executable_regional_plan(regional):
                    failed = "; ".join(
                        f"{c.label}: {c.status} {c.reason}"
                        for c in regional.checks
                        if c.status != "passed"
                    )
                    return evidence.model_copy(
                        update={
                            "status": combined_status([regional.status, "unknown"]),
                            "reason": f"{name} 滚动设备计划未满足采用条件：{regional.reason} {failed}",
                        }
                    )
            # Check the adopted regional outputs jointly, rather than trusting
            # capacity feasibility of the ADMM reference alone.
            aggregate = np.sum([r.p_mw for r, _ in plans.values()], axis=0)
            if (
                aggregate.max()
                > window.cluster_import_limit_mw + self.policy.numerical_tolerance
            ):
                return evidence.model_copy(
                    update={
                        "status": "violated",
                        "reason": "滚动区域计划合计受电超过集群容量，停止采用",
                    }
                )
            for mg in self.case.microgrids:
                regional, plan = plans[mg.name]
                assert plan is not None
                if mg.name not in self.sessions:
                    session = device_tracking_session(
                        self.case,
                        mg,
                        initial_plan(plan, mg.name, len(self.case.time_hours)),
                        plant_inputs=self.plants[mg.name],
                        storage_enabled=self.storage_enabled,
                        config=execution_time_scale(self.policy),
                        seed=self.policy.random_seed + self.names.index(mg.name),
                    )
                    self.sessions[mg.name] = session
                    self.checkpoints[mg.name] = next(session)
                self.commands[mg.name] = first_command(regional, start, end - start)
                if minute_replan:
                    self.commands[mg.name] = DevicePlanUpdate(
                        start_minute=start,
                        p_mw=regional.p_mw,
                        q_mvar=regional.q_mvar,
                        resource_p_mw=regional.resource_p_mw,
                        resource_q_mvar=regional.resource_q_mvar,
                    )
                else:
                    self.current_terminal[mg.name] = regional.storage_energy_mwh[1]
                self.accepted[mg.name].append(regional)
            self.last_reference, self.last_start = reference, start
            self.last_step = planning_step
            self.accepted_times.append(float(start))
            return evidence.model_copy(
                update={
                    "adopted": True,
                    "status": combined_status([r.status for r, _ in plans.values()]),
                    "adopted_p_mw": {n: r.p_mw[0] for n, (r, _) in plans.items()},
                    "adopted_q_mvar": {n: r.q_mvar[0] for n, (r, _) in plans.items()},
                }
            )
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            return evidence.model_copy(
                update={"reason": f"滚动更新失败，停止采用：{exc}"}
            )

    def advance(self, start: int, end: int) -> dict[str, float]:
        for minute in range(start, end):
            if any(self.checkpoints[name].minute != minute for name in self.names):
                raise ValueError("regional execution clocks differ")
            self._minute_reserve_feedback(minute, start, end)
            for name in self.names:
                try:
                    checkpoint = self.sessions[name].send(
                        self.commands[name]
                        if minute == self.commands[name].start_minute
                        else None
                    )
                    self.checkpoints[name] = checkpoint
                    self.energies[name] = checkpoint.storage_energy_mwh
                except StopIteration as done:
                    if minute != self.total - 1:
                        raise RuntimeError(
                            "device session stopped before the study end"
                        ) from done
                    self.results[name] = done.value
                    self.energies[name] = float(done.value.storage_energy_mwh[-1])
        return dict(self.energies)

    def _minute_reserve_feedback(self, minute: int, start: int, end: int) -> None:
        policy = self.policy.storage_reserve
        if policy is None or not self.storage_enabled:
            return
        observations: list[MinuteReserveObservation] = []
        for mg in self.minute_forecast.microgrids:
            scale = sum(
                float(v[minute])
                for v in (
                    *mg.load_p_mw.values(),
                    *mg.wind_available_mw.values(),
                    *mg.pv_available_mw.values(),
                )
            )
            st = mg.storage
            observations.append(
                observe_storage_reserve(
                    region=mg.name,
                    state=self.checkpoints[mg.name],
                    required_mw=policy.minimum_error_mw
                    + policy.forecast_error_fraction * scale,
                    minimum_energy_mwh=st.e_min_mwh,
                    maximum_energy_mwh=st.e_max_mwh,
                    maximum_power_mw=st.p_max_mw,
                    eta_charge=st.eta_charge,
                    eta_discharge=st.eta_discharge,
                    remaining_minutes=self.total - minute,
                    policy=policy,
                )
            )
        deficient = tuple(o.region for o in observations if o.deficient)
        replan = (
            bool(deficient)
            and minute > start
            and minute - self.last_reserve_replan >= policy.replan_cooldown_minutes
        )
        self.minute_reserves.extend(
            o.model_copy(
                update={
                    "action": "replan"
                    if replan
                    else "recovering"
                    if o.deficient
                    else "monitor",
                }
            )
            for o in observations
        )
        if not replan:
            return
        # Snapshot issued commands BEFORE any replacement. No completed target is edited.
        original_p = {
            n: c.p_mw[minute - c.start_minute :] for n, c in self.commands.items()
        }
        original_q = {
            n: c.q_mvar[minute - c.start_minute :] for n, c in self.commands.items()
        }
        self.progress(f"第 {minute} 分钟备用不足，联合重算至第 {end} 分钟的后续目标")
        update = self.prepare(minute, end, end, minute_replan=True)
        self.reserve_target_adjustments.append(
            ReserveTargetAdjustment(
                minute=minute,
                trigger_regions=deficient,
                original_p_mw=original_p,
                original_q_mvar=original_q,
                adopted_p_mw={n: c.p_mw for n, c in self.commands.items()}
                if update.adopted
                else {},
                adopted_q_mvar={n: c.q_mvar for n, c in self.commands.items()}
                if update.adopted
                else {},
                update=update,
            )
        )
        self.last_reserve_replan = minute
        if not update.adopted:
            raise RuntimeError(
                f"第 {minute} 分钟备用反馈未获可执行目标：{update.reason}"
            )

    def run(self) -> None:
        try:
            self.updates = run_feedback_loop(
                total_minutes=self.total,
                policy=self.policy,
                prepare=self.prepare,
                advance=self.advance,
                progress=self.progress,
                rolling_progress=self.rolling_progress,
            )
        finally:
            for name, session in self.sessions.items():
                if name not in self.results and self.checkpoints[name].minute > 0:
                    try:
                        session.throw(StopDeviceSession())
                    except StopIteration as done:
                        if done.value is not None:
                            self.results[name] = done.value
                    except (StopDeviceSession, ValueError, RuntimeError):
                        # An already-failed plant cannot certify additional output.
                        pass
                session.close()

    def intraday_evidence(self) -> tuple[RegionalExecution, ...]:
        result: list[RegionalExecution] = []
        complete = (
            bool(self.updates)
            and self.updates[-1].end_minute == self.total
            and bool(self.updates[-1].actual_end_energy_mwh)
        )
        for name in self.names:
            rows = self.accepted[name]
            checks = [
                ExecutionCheck(
                    code="ROLLING_FEEDBACK",
                    label="预测与实际电量滚动反馈",
                    status="passed" if complete else "unknown",
                    reason=""
                    if complete
                    else (
                        self.updates[-1].reason if self.updates else "未获得滚动计划"
                    ),
                )
            ]
            if not rows:
                result.append(
                    RegionalExecution(
                        region=name,
                        status="unknown",
                        checks=tuple(checks),
                        reason=checks[0].reason,
                    )
                )
                continue
            for code in sorted({c.code for row in rows for c in row.checks}):
                matching = [c for row in rows for c in row.checks if c.code == code]
                first = matching[0]
                if code in ("P_TRACKING", "Q_TRACKING"):
                    actual = max(
                        abs(r.p_mw[0] - r.p_target_mw[0])
                        if code == "P_TRACKING"
                        else abs(r.q_mvar[0] - r.q_target_mvar[0])
                        for r in rows
                    )
                    passed = (
                        self.policy.tracking_limits.accepts_p(actual)
                        if code == "P_TRACKING"
                        else self.policy.tracking_limits.accepts_q(actual)
                    )
                    checks.append(
                        first.model_copy(
                            update={
                                "status": "passed" if passed else "violated",
                                "actual": actual,
                            }
                        )
                    )
                else:
                    checks.append(
                        first.model_copy(
                            update={
                                "status": combined_status([c.status for c in matching]),
                                "actual": max(
                                    (
                                        c.actual
                                        for c in matching
                                        if c.actual is not None
                                    ),
                                    default=None,
                                ),
                            }
                        )
                    )
            result.append(
                RegionalExecution(
                    region=name,
                    status=combined_status([c.status for c in checks]),
                    checks=tuple(checks),
                    reason=checks[0].reason,
                    economic_cost_cny=None,
                    time_minutes=tuple(self.accepted_times),
                    p_mw=tuple(r.p_mw[0] for r in rows),
                    q_mvar=tuple(r.q_mvar[0] for r in rows),
                    p_target_mw=tuple(r.p_target_mw[0] for r in rows),
                    q_target_mvar=tuple(r.q_target_mvar[0] for r in rows),
                    storage_power_mw=tuple(r.storage_power_mw[0] for r in rows),
                    storage_energy_mwh=tuple(r.storage_energy_mwh[0] for r in rows),
                    resource_p_mw={
                        key: tuple(r.resource_p_mw[key][0] for r in rows)
                        for key in rows[0].resource_p_mw
                    },
                    resource_q_mvar={
                        key: tuple(r.resource_q_mvar[key][0] for r in rows)
                        for key in rows[0].resource_q_mvar
                    },
                )
            )
        return tuple(result)

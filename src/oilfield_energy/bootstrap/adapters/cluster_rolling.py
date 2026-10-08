"""Numerical bridge for causal, synchronized cluster rolling execution."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from math import gcd, isclose, ceil

import numpy as np
from numpy.typing import NDArray

from oilfield_energy.admm import CoordinationWorkspace, run_admm_coordination
from oilfield_energy.data import ProjectCase
from oilfield_energy.hierarchy_types import (
    ADMMConfig,
    ADMMResult,
    ClusterReference,
    CommunicationConfig,
)
from oilfield_energy.model import OptimizationResult
from oilfield_energy.modules.control.contracts import (
    DeviceCheckpoint,
    DynamicTrackingPolicy,
    PlantInputs,
)
from oilfield_energy.modules.dispatch.api import (
    slice_coordination_calibration,
    cluster_import_tracking_margin,
)
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    RegionalNetworkCandidate,
    StorageReachabilityInput,
    StorageReachabilityPolicy,
    ReserveRecoveryTask,
)
from oilfield_energy.planning_security import build_planning_security_trajectories
from oilfield_energy.workflows.cluster_execution.api import (
    executable_regional_plan,
    run_feedback_loop,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    RegionalExecution,
    RollingProgress,
    RollingUpdate,
)

from .cluster_reactive import with_reactive_planning
from .cluster_recovery import RegionalDiagnostics, solve_with_reference_recovery
from .cluster_reserve import with_storage_reserves
from .cluster_safety import ClusterSafetyPort, build_cluster_safety_domain
from oilfield_energy.workflows.cluster_execution.contracts import (
    ClusterSafetyDomain,
    ClusterSafetyEvidence,
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
        storage_reachability={},
        storage_window_targets={},
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
        storage_reachability={},
        storage_window_targets={},
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


def reserve_forecast_tail(case: ProjectCase, horizon_end: int) -> ProjectCase:
    """Explicit constant-last FORECAST extension, never future plant telemetry."""
    step = case.assumptions.dt_hours * 60
    count = int(round(horizon_end / step))
    if count <= len(case.time_hours):
        return case
    indices = np.minimum(np.arange(count), len(case.time_hours) - 1)
    return replace(
        case,
        time_hours=np.arange(count) * step / 60,
        price_cny_per_mwh=case.price_cny_per_mwh[indices].copy(),
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


def with_storage_reachability(
    case: ProjectCase,
    *,
    policy: StorageReachabilityPolicy,
    dynamics: DynamicTrackingPolicy,
    checkpoints: Mapping[str, DeviceCheckpoint],
    storage_enabled: bool,
    execution_step_minutes: float,
) -> ProjectCase:
    """Project completed states; only startup may declare initialization at its plan."""
    if not storage_enabled:
        return replace(case, storage_reachability={})
    start = float(case.time_hours[0] * 60)
    names = {mg.name for mg in case.microgrids}
    if (checkpoints and set(checkpoints) != names) or (start != 0 and not checkpoints):
        raise ValueError(
            "storage reachability requires all current regional checkpoints"
        )
    inputs = {}
    for mg in case.microgrids:
        st = mg.storage
        observed = checkpoints.get(mg.name)
        if observed is not None and (
            not isclose(observed.minute, start, rel_tol=0, abs_tol=1e-8)
            or abs(observed.storage_energy_mwh - st.e_initial_mwh)
            > policy.energy_tolerance_mwh
        ):
            raise ValueError(
                "storage checkpoint clock/energy differs from the planning boundary"
            )
        inputs[mg.name] = StorageReachabilityInput(
            policy=policy,
            resource_id=mg.resource_id("storage", st.bus),
            start_minute=start,
            steps=len(case.time_hours),
            planning_minutes=case.assumptions.dt_hours * 60,
            execution_minutes=execution_step_minutes,
            time_constant_minutes=dynamics.time_constant_minutes,
            ramp_mw_per_minute=dynamics.p_ramp_mw_per_minute,
            maximum_power_mw=st.p_max_mw,
            initial_power_mw=observed.storage_power_mw
            if observed is not None
            else None,
            initial_energy_mwh=st.e_initial_mwh,
            minimum_energy_mwh=st.e_min_mwh,
            maximum_energy_mwh=st.e_max_mwh,
            terminal_energy_mwh=st.terminal_energy_mwh,
            terminal_tolerance_mwh=case.assumptions.terminal_energy_tolerance_mwh,
            eta_charge=st.eta_charge,
            eta_discharge=st.eta_discharge,
        )
    return replace(case, storage_reachability=inputs)


class RollingClusterEngine:
    def __init__(
        self,
        *,
        case: ProjectCase,
        original_case: ProjectCase,
        plants: dict[str, PlantInputs],
        original_admm: ClusterReference,
        day_ahead: tuple[RegionalExecution, ...],
        storage_enabled: bool,
        policy: ExecutionPolicy,
        admm_config: ADMMConfig,
        communication_config: CommunicationConfig,
        loss_calibration: Mapping[
            str, CoordinationNetworkCalibration | Mapping[str, np.ndarray]
        ]
        | None,
        solve_regions: Callable[
            [ProjectCase, ClusterReference],
            dict[str, tuple[RegionalExecution, OptimizationResult | None]],
        ],
        progress: Callable[[str], None],
        rolling_progress: Callable[[RollingProgress], None] | None = None,
        diagnose_regions: RegionalDiagnostics | None = None,
    ):
        if policy.cluster_safety is None:
            raise ValueError("cluster execution requires the certified safety policy")
        original_step = original_case.assumptions.dt_hours * 60
        if not float(original_step).is_integer():
            raise ValueError("rolling execution requires integer minute inputs")
        # Quarter-hour defaults also support coarser 24-point forecasts without
        # using the minute plant as a forecast source.
        self.step = gcd(int(original_step), policy.update_minutes)
        self.policy = policy.model_copy(
            update={
                "update_minutes": self.step,
                "cluster_transition_version": "cluster-continuation-v1",
            }
        )
        self.case = forecast_grid(case, self.step)
        self.original_case, self.original_admm = original_case, original_admm
        self.plants, self.storage_enabled = plants, storage_enabled
        self.admm_config, self.communication_config = admm_config, communication_config
        if policy.network_coordination is not None:
            self.admm_config = replace(admm_config, network_model="branch-flow-socp-v1")
        self.loss_calibration = loss_calibration
        self.solve_regions, self.progress = solve_regions, progress
        self.diagnose_regions = diagnose_regions
        self.rolling_progress = rolling_progress
        self.total = len(self.case.time_hours) * self.step
        self.names = tuple(m.name for m in self.case.microgrids)
        self.day_ahead = {r.region: r for r in day_ahead}
        self.energies = {m.name: m.storage.e_initial_mwh for m in self.case.microgrids}
        self.checkpoints: dict[str, DeviceCheckpoint] = {}
        self.updates: tuple[RollingUpdate, ...] = ()
        self.last_reference: ClusterReference | None = None
        self.last_start = 0
        self.last_step = self.step
        self.current_terminal: dict[str, float] = {}
        self.reserve_tasks: dict[tuple[str, str], ReserveRecoveryTask] = {}
        self.reserve_recovery_history: list[ReserveRecoveryTask] = []
        self.reserve_observations = []
        self.minute_forecast = forecast_grid(case, 1)
        self.coordination_workspace = (
            CoordinationWorkspace() if policy.coordination_model_reuse else None
        )
        self.safety: ClusterSafetyPort | None = None
        self.safety_domain: ClusterSafetyDomain | None = None
        self.safety_failure = ""
        try:
            self.safety_domain = build_cluster_safety_domain(
                self.case, self.policy, storage_enabled
            )
            self.safety = ClusterSafetyPort(
                self.case,
                self.policy,
                storage_enabled,
                plants,
                domain=self.safety_domain,
            )
            self._sync_safety()
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            self.safety = None
            self.safety_failure = str(exc)

    def safety_evidence(self) -> ClusterSafetyEvidence | None:
        if self.safety is not None:
            return self.safety.evidence()
        if self.safety_domain is not None:
            return ClusterSafetyEvidence(
                domain=self.safety_domain, failure=self.safety_failure
            )
        return None

    def _sync_safety(self) -> None:
        assert self.safety is not None
        self.checkpoints = self.safety.checkpoints()
        self.energies = {n: s.storage_energy_mwh for n, s in self.checkpoints.items()}
        c = self.safety.controller
        self.reserve_tasks = dict(c.tasks)
        self.reserve_recovery_history = list(c.task_history)
        self.reserve_observations = list(c.observations)

    def prepare(
        self,
        start: int,
        end: int,
        horizon_end: int,
        *,
        minute_replan: bool = False,
    ) -> RollingUpdate:
        if self.safety is None:
            return RollingUpdate(
                start_minute=start,
                end_minute=end,
                horizon_end_minute=horizon_end,
                status="unknown",
                reason="初始联合可持续控制未获证明：" + self.safety_failure,
            )
        try:
            update = self._prepare_candidate(
                start,
                end,
                horizon_end,
                minute_replan=minute_replan,
            )
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            update = RollingUpdate(
                start_minute=start,
                end_minute=end,
                horizon_end_minute=horizon_end,
                status="unknown",
                reason="经济重规划未成功：" + str(exc),
            )
        if self.safety is not None and not update.adopted:
            # Economic failure never discards a previously certified continuation.
            self.safety.plans = {}
            return update.model_copy(
                update={
                    "continuation_available": True,
                    "initial_energy_mwh": dict(self.energies),
                    "initial_storage_power_mw": {
                        n: c.storage_power_mw for n, c in self.checkpoints.items()
                    },
                    "reserve_tasks": tuple(self.reserve_tasks.values()),
                }
            )
        return update

    def _prepare_candidate(
        self,
        start: int,
        end: int,
        horizon_end: int,
        *,
        minute_replan: bool = False,
    ) -> RollingUpdate:
        reserve = self.policy.storage_reserve
        delivery = self.policy.reserve_delivery
        if self.storage_enabled and reserve is not None and delivery is not None:
            self._observe_recovery(start)
            deadline = max(
                (
                    t.deadline_minute
                    for t in self.reserve_tasks.values()
                    if t.status != "recovered"
                ),
                default=start,
            )
            horizon_end = max(
                horizon_end,
                end + reserve.support_minutes + delivery.response_minutes,
                deadline + reserve.support_minutes + delivery.response_minutes,
            )
            if not minute_replan:
                horizon_end = ceil(horizon_end / self.step) * self.step
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
            reserve_tasks=tuple(self.reserve_tasks.values()),
            initial_energy_mwh=dict(self.energies),
            initial_storage_power_mw={
                n: s.storage_power_mw for n, s in self.checkpoints.items()
            },
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
            )
            + (
                "；备用证明越过研究终点的预测尾段采用末值保持延拓"
                if horizon_end > self.total
                else ""
            ),
        )
        try:
            planning_step = 1 if minute_replan else self.step
            source = self.minute_forecast if minute_replan else self.case
            if delivery is not None and reserve is not None and self.storage_enabled:
                # Exact integer-minute deadline nodes; no rounding a deadline later.
                planning_step = gcd(planning_step, horizon_end - start)
                for task in self.reserve_tasks.values():
                    if task.status != "recovered" and task.deadline_minute > start:
                        planning_step = gcd(planning_step, task.deadline_minute - start)
                source = forecast_grid(self.case, planning_step)
                source = reserve_forecast_tail(source, horizon_end)
            window = window_case(
                source,
                start // planning_step,
                horizon_end // planning_step,
                self.energies,
                terminals,
            )
            if self.storage_enabled and self.policy.storage_reserve is not None:
                window = with_storage_reserves(
                    window,
                    self.policy.storage_reserve,
                    max(self.total, horizon_end) - start,
                    start_minute=start,
                    tasks=self.reserve_tasks if delivery is not None else None,
                    full_support=delivery is not None,
                    delivery_ramp_mw=self.policy.dynamic_tracking.p_ramp_mw_per_minute
                    if self.policy.dynamic_tracking is not None
                    else None,
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
            if self.safety is not None:
                window = self.safety.planning_case(window)
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
                    execution_step_minutes=self.policy.device_step_minutes,
                )
            if self.policy.storage_reachability is not None:
                if self.policy.dynamic_tracking is None:
                    raise ValueError(
                        "storage reachability requires explicit response dynamics"
                    )
                window = with_storage_reachability(
                    window,
                    policy=self.policy.storage_reachability,
                    dynamics=self.policy.dynamic_tracking,
                    checkpoints=self.checkpoints,
                    storage_enabled=self.storage_enabled,
                    execution_step_minutes=self.policy.device_step_minutes,
                )
            security = build_planning_security_trajectories(window, list(self.names))
            indices = np.minimum(
                len(self.original_case.time_hours) - 1,
                np.floor(
                    window.time_hours / self.original_case.assumptions.dt_hours
                ).astype(int),
            )
            losses = (
                None
                if self.loss_calibration is None
                else {
                    name: (
                        slice_coordination_calibration(row, tuple(map(int, indices)))
                        if isinstance(row, CoordinationNetworkCalibration)
                        else {
                            key: copy_series(values[indices])
                            for key, values in row.items()
                        }
                    )
                    for name, row in self.loss_calibration.items()
                }
            )

            def coordinate(
                current: Mapping[str, CoordinationNetworkCalibration] | None,
                margin: tuple[float, ...],
                candidates: tuple[RegionalNetworkCandidate, ...] = (),
            ) -> ADMMResult:
                return run_admm_coordination(
                    replace(
                        window,
                        cluster_import_limit_mw=window.cluster_import_limit_mw
                        - cluster_import_tracking_margin(
                            region_count=len(self.names),
                            tracking=self.policy.tracking_limits,
                        ),
                    ),
                    self.names,
                    admm_config=replace(
                        self.admm_config, aggregate_pf_margin_mvar=margin
                    ),
                    communication_config=self.communication_config,
                    p_grid_security_floors_mw={
                        n: security[n].effective_floor_mw for n in self.names
                    },
                    storage_enabled=self.storage_enabled,
                    loss_calibration=current,
                    network_candidates={c.region: c for c in candidates}
                    if candidates
                    else None,
                    workspace=self.coordination_workspace,
                )

            recovered = solve_with_reference_recovery(
                case=window,
                policy=self.policy,
                calibration=losses,
                coordinate=coordinate,
                coordinate_candidates=coordinate,
                aggregate_pf_target=self.admm_config.aggregate_pf_target,
                solve_regions=self.solve_regions,
                diagnose_regions=self.diagnose_regions,
                storage_enabled=self.storage_enabled,
            )
            evidence = evidence.model_copy(
                update={"reference_recovery": recovered.report}
            )
            if recovered.payload is None:
                return evidence.model_copy(update={"reason": recovered.report.reason})
            reference, plans = recovered.payload.reference, recovered.payload.plans
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
            evidence = evidence.model_copy(
                update={
                    "regional_checks": {n: r.checks for n, (r, _) in plans.items()},
                    "optimization_quality": {
                        n: r.optimization_quality for n, (r, _) in plans.items()
                    },
                    "ac_recovery": {
                        n: r.ac_recovery
                        for n, (r, _) in plans.items()
                        if r.ac_recovery is not None
                    },
                }
            )
            if not recovered.report.accepted:
                return evidence.model_copy(
                    update={
                        "status": "violated"
                        if recovered.report.stop_reason == "joint_rejected"
                        else "unknown",
                        "reason": f"{recovered.report.stop_reason}：{recovered.report.reason}",
                    }
                )
            assert self.safety is not None
            self.safety.plans = {n: r for n, (r, _) in plans.items()}
            self.safety.plan_start = start
            proposal = self.safety.propose()
            assert proposal is not None
            try:
                self.safety.controller.certify_candidate(proposal)
            except ValueError as exc:
                return evidence.model_copy(
                    update={"reason": "联合连续安全/后续可行性未获证明：" + str(exc)}
                )
            self.last_reference, self.last_start = reference, start
            self.last_step = planning_step
            self.current_terminal = dict(terminals)
            return evidence.model_copy(
                update={
                    "adopted": True,
                    "continuation_available": True,
                    "status": "passed",
                    "adopted_p_mw": {n: r.p_mw[0] for n, (r, _) in plans.items()},
                    "adopted_q_mvar": {n: r.q_mvar[0] for n, (r, _) in plans.items()},
                }
            )
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            return evidence.model_copy(
                update={"reason": f"滚动更新失败，停止采用：{exc}"}
            )

    def advance(self, start: int, end: int) -> dict[str, float]:
        if self.safety is None:
            raise RuntimeError(
                "cluster safety initialization failed: " + self.safety_failure
            )
        try:
            for minute in range(start, end):
                if minute != self.safety.controller.minute:
                    raise ValueError(
                        "joint safety clock differs from rolling execution"
                    )
                self.safety.advance(
                    "采用保留的认证后续策略" if not self.safety.plans else ""
                )
                self._sync_safety()
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            self.safety.controller.failure = str(exc)
            raise
        return dict(self.energies)

    def _observe_recovery(self, minute: int) -> bool:
        """Only the current certified controller state can close reserve debt."""
        if self.safety is None:
            raise RuntimeError("cluster safety is unavailable")
        if minute != self.safety.controller.minute:
            raise ValueError("reserve observation must use the current execution clock")
        certificate = self.safety.controller.observe_reserve()
        self._sync_safety()
        return certificate is None or certificate.status == "certified"

    def run(self) -> None:
        self.updates = run_feedback_loop(
            total_minutes=self.total,
            policy=self.policy,
            prepare=self.prepare,
            advance=self.advance,
            progress=self.progress,
            rolling_progress=self.rolling_progress,
        )

    def intraday_evidence(self) -> tuple[RegionalExecution, ...]:
        if self.safety is not None:
            return tuple(self.safety.minute_evidence(n, self.total) for n in self.names)
        return tuple(
            RegionalExecution(region=n, status="unknown", reason=self.safety_failure)
            for n in self.names
        )

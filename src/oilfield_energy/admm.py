"""集群—区域两级P/Q协调的共识ADMM实现。"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Dict, Mapping, Sequence

import numpy as np

from .communication import SimulatedCommunicationChannel
from .data import MicrogridData, ProjectCase
from .hierarchy_types import (
    ADMMConfig,
    ADMMIteration,
    ADMMResult,
    CommunicationConfig,
    CoordinationSignal,
    GroupControlConfig,
    RegionalSchedule,
)
from .modules.control.api import accept_coordination_message
from .modules.control.contracts import MessageCursor
from .modules.dispatch.api import capture_coordination_snapshot
from .modules.dispatch.contracts import (
    CoordinationIterationTrace,
    DispatchCapabilities,
    RegionalCoordinationTrace,
    RegionalPlanSnapshot,
)
from .planning_security import resolve_security_floors
from .regional_control import ClusterProjectionController, RegionalConvexController


def _snapshot_plan(schedule: RegionalSchedule) -> RegionalPlanSnapshot:
    """Adapt legacy array output to the owned public snapshot contract."""
    return RegionalPlanSnapshot(
        **{
            item.name: tuple(float(v) for v in value) if isinstance(value, np.ndarray) else value
            for item in fields(schedule)
            for value in (getattr(schedule, item.name),)
        }
    )


def _legacy_plan(plan: RegionalPlanSnapshot) -> RegionalSchedule:
    """Compatibility copy: mutating it cannot change certified evidence."""
    return RegionalSchedule(
        **{
            item.name: np.array(value, dtype=float) if isinstance(value, tuple) else value
            for item in fields(plan)
            for value in (getattr(plan, item.name),)
        }
    )


def _signal(
    z_p: np.ndarray, z_q: np.ndarray, u_p: np.ndarray, u_q: np.ndarray, m: int, k: int
) -> CoordinationSignal:
    return CoordinationSignal(
        p_reference_mw=z_p[m].copy(),
        q_reference_mvar=z_q[m].copy(),
        dual_p=u_p[m].copy(),
        dual_q=u_q[m].copy(),
        iteration=k,
    )


def run_admm_coordination(
    case: ProjectCase,
    microgrid_names: Sequence[str] | None = None,
    *,
    admm_config: ADMMConfig | None = None,
    communication_config: CommunicationConfig | None = None,
    loss_calibration: Mapping[str, Mapping[str, np.ndarray]] | None = None,
    p_grid_security_floors_mw: Mapping[str, np.ndarray] | None = None,
    group_control_config: GroupControlConfig | None = None,
    storage_enabled: bool = True,
) -> ADMMResult:
    """运行逻辑隔离的区域控制器和集群协调器。"""
    config = admm_config or ADMMConfig()
    comm_config = communication_config or CommunicationConfig()
    capabilities = DispatchCapabilities(storage_enabled=storage_enabled)
    if config.max_iterations < 1 or config.consecutive_convergence_iterations < 1:
        raise ValueError("ADMM iteration limits must be positive")
    if comm_config.stale_limit_iterations < 0:
        raise ValueError("stale limit must be nonnegative")
    lookup = {mg.name: mg for mg in case.microgrids}
    names = list(microgrid_names) if microgrid_names is not None else list(lookup)
    if len(names) < 2:
        raise ValueError("ADMM cluster coordination requires at least two microgrids")
    if len(set(names)) != len(names):
        raise ValueError("ADMM region names must be unique")
    microgrids: list[MicrogridData] = [lookup[name] for name in names]
    security_floors = resolve_security_floors(
        case,
        microgrids,
        p_grid_security_floors_mw,
        config=group_control_config,
    )
    controllers = {
        mg.name: RegionalConvexController(
            case,
            mg,
            config,
            loss_calibration=loss_calibration.get(mg.name)
            if loss_calibration is not None
            else None,
            p_grid_security_floor_mw=security_floors[mg.name],
            capabilities=capabilities,
        )
        for mg in microgrids
    }
    coordinator = ClusterProjectionController(
        case,
        microgrids,
        config,
        p_grid_security_floors_mw=security_floors,
    )
    channel = SimulatedCommunicationChannel(comm_config)
    M, T = len(names), len(case.time_hours)

    autonomous = {name: controllers[name].solve(None, fallback=True) for name in names}
    latest_schedules: Dict[str, RegionalSchedule] = dict(autonomous)
    x_p = np.vstack([autonomous[name].p_grid_mw for name in names])
    x_q = np.vstack([autonomous[name].q_grid_mvar for name in names])
    z_p, z_q = coordinator.project(x_p, x_q)
    u_p = np.zeros((M, T))
    u_q = np.zeros((M, T))
    coordination_epoch = 0
    cached_signals = {
        name: _signal(z_p, z_q, u_p, u_q, m, coordination_epoch) for m, name in enumerate(names)
    }
    signal_cursors = {name: MessageCursor(0, 0) for name in names}
    response_cursors = {name: MessageCursor(-1, -1) for name in names}
    history: list[ADMMIteration] = []
    converged = False
    stop_reason = "maximum_iterations"
    convergence_streak = 0
    pending_responses: Dict[str, RegionalSchedule] = {}
    pending_sent: Dict[str, int] = {}
    latest_proposal_sent: dict[str, int] = {}
    coordination_updates = 0
    snapshot = None

    for k in range(1, config.max_iterations + 1):
        fallback_regions: list[str] = []
        for m, name in enumerate(names):
            channel.send(
                "coordinator",
                name,
                _signal(z_p, z_q, u_p, u_q, m, coordination_epoch),
                k,
            )

        for name in names:
            messages = channel.receive(name, k)
            accepted = False
            for message in messages:
                cursor = MessageCursor(message.payload.iteration, message.sent_iteration)
                if accept_coordination_message(
                    signal_cursors[name],
                    cursor,
                    now_tick=k,
                    maximum_age_ticks=comm_config.stale_limit_iterations,
                ):
                    cached_signals[name] = message.payload
                    signal_cursors[name] = cursor
                    accepted = True
                else:
                    channel.metrics.rejected_stale_messages += 1
            if not accepted:
                channel.metrics.stale_uses += 1
            if k - signal_cursors[name].sent_tick > comm_config.stale_limit_iterations:
                schedule = controllers[name].solve(None, fallback=True)
                fallback_regions.append(name)
                channel.metrics.fallback_uses += 1
            else:
                schedule = controllers[name].solve(cached_signals[name], fallback=False)
            channel.send(name, "coordinator", schedule, k)

        for name in tuple(pending_responses):
            if k - pending_sent[name] > comm_config.stale_limit_iterations:
                del pending_responses[name]
                del pending_sent[name]
        for message in channel.receive("coordinator", k):
            if message.sender in latest_schedules:
                schedule = message.payload
                previous = response_cursors[message.sender]
                # A newer autonomous status invalidates pending coordinated work;
                # it never replaces a coordinated plan with epoch -1.
                if schedule.used_fallback:
                    if (
                        message.sent_iteration > previous.sent_tick
                        and 0 <= k - message.sent_iteration <= comm_config.stale_limit_iterations
                    ):
                        response_cursors[message.sender] = MessageCursor(
                            previous.epoch, message.sent_iteration
                        )
                        pending_responses.pop(message.sender, None)
                        pending_sent.pop(message.sender, None)
                    else:
                        channel.metrics.rejected_stale_messages += 1
                    continue
                cursor = MessageCursor(schedule.signal_iteration, message.sent_iteration)
                if not accept_coordination_message(
                    previous,
                    cursor,
                    now_tick=k,
                    maximum_age_ticks=comm_config.stale_limit_iterations,
                ):
                    channel.metrics.rejected_stale_messages += 1
                    continue
                response_cursors[message.sender] = cursor
                latest_schedules[message.sender] = schedule
                latest_proposal_sent[message.sender] = message.sent_iteration
                if schedule.signal_iteration == coordination_epoch:
                    pending_responses[message.sender] = schedule
                    pending_sent[message.sender] = message.sent_iteration

        global_updated = len(pending_responses) == M
        fresh_regions = frozenset(pending_responses)
        collected_epoch = coordination_epoch
        z_p_previous = z_p.copy()
        z_q_previous = z_q.copy()
        if global_updated:
            x_p = np.vstack([pending_responses[name].p_grid_mw for name in names])
            x_q = np.vstack([pending_responses[name].q_grid_mvar for name in names])
            z_p, z_q = coordinator.project(x_p + u_p, x_q + u_q)
            u_p = u_p + x_p - z_p
            u_q = u_q + x_q - z_q
            snapshot = capture_coordination_snapshot(
                epoch=coordination_epoch,
                communication_tick=k,
                capabilities=capabilities,
                regions=tuple(_snapshot_plan(pending_responses[name]) for name in names),
                p_references_mw=z_p,
                q_references_mvar=z_q,
                previous_p_references_mw=z_p_previous,
                previous_q_references_mvar=z_q_previous,
                dual_p=u_p,
                dual_q=u_q,
                rho=config.rho,
                absolute_tolerance=config.absolute_tolerance,
                relative_tolerance=config.relative_tolerance,
            )
            coordination_updates += 1
            coordination_epoch += 1
            pending_responses.clear()
            pending_sent.clear()
        else:
            x_p = np.vstack([latest_schedules[name].p_grid_mw for name in names])
            x_q = np.vstack([latest_schedules[name].q_grid_mvar for name in names])

        primal = float(np.sqrt(np.sum((x_p - z_p) ** 2) + np.sum((x_q - z_q) ** 2)))
        dual = (
            float(
                config.rho
                * np.sqrt(np.sum((z_p - z_p_previous) ** 2) + np.sum((z_q - z_q_previous) ** 2))
            )
            if global_updated
            else 0.0
        )
        dimension = 2 * M * T
        eps_primal = float(
            np.sqrt(dimension) * config.absolute_tolerance
            + config.relative_tolerance
            * max(
                np.sqrt(np.sum(x_p**2) + np.sum(x_q**2)),
                np.sqrt(np.sum(z_p**2) + np.sum(z_q**2)),
            )
        )
        eps_dual = float(
            np.sqrt(dimension) * config.absolute_tolerance
            + config.relative_tolerance * config.rho * np.sqrt(np.sum(u_p**2) + np.sum(u_q**2))
        )
        if global_updated:
            primal = snapshot.residuals.primal
            dual = snapshot.residuals.dual
            eps_primal = snapshot.residuals.primal_tolerance
            eps_dual = snapshot.residuals.dual_tolerance
        fully_fresh = global_updated
        residuals_met = k >= config.min_iterations and primal <= eps_primal and dual <= eps_dual
        if global_updated:
            if residuals_met and fully_fresh:
                convergence_streak += 1
            else:
                convergence_streak = 0
        history.append(
            ADMMIteration(
                iteration=k,
                primal_residual=primal,
                dual_residual=dual,
                primal_tolerance=eps_primal,
                dual_tolerance=eps_dual,
                local_objective_cny=float(
                    sum(
                        schedule.local_objective_cny
                        for schedule in (
                            snapshot.regions if global_updated else latest_schedules.values()
                        )
                    )
                ),
                fresh_region_count=M if global_updated else len(pending_responses),
                convergence_streak=convergence_streak,
                fallback_regions=fallback_regions,
                coordination=CoordinationIterationTrace(
                    communication_tick=k,
                    coordination_epoch=collected_epoch,
                    global_updated=global_updated,
                    time_hours=tuple(float(hour) for hour in case.time_hours),
                    regions=tuple(
                        RegionalCoordinationTrace(
                            region=name,
                            response_epoch=latest_schedules[name].signal_iteration
                            if name in latest_proposal_sent else None,
                            response_sent_tick=latest_proposal_sent.get(name),
                            fresh=name in fresh_regions,
                            outage=channel.region_outage_at(name, k),
                            fallback=name in fallback_regions,
                            proposal_p_mw=tuple(float(v) for v in latest_schedules[name].p_grid_mw)
                            if name in latest_proposal_sent else None,
                            proposal_q_mvar=tuple(float(v) for v in latest_schedules[name].q_grid_mvar)
                            if name in latest_proposal_sent else None,
                            reference_p_mw=tuple(float(v) for v in z_p[m]),
                            reference_q_mvar=tuple(float(v) for v in z_q[m]),
                        )
                        for m, name in enumerate(names)
                    ),
                ),
            )
        )
        if convergence_streak >= config.consecutive_convergence_iterations:
            converged = True
            stop_reason = "residual_tolerances_met"
            break

    references_p = {name: z_p[m].copy() for m, name in enumerate(names)}
    references_q = {name: z_q[m].copy() for m, name in enumerate(names)}
    if snapshot is not None:
        snapshot = replace(snapshot, converged=converged)
        published_schedules = {plan.name: _legacy_plan(plan) for plan in snapshot.regions}
    else:
        published_schedules = latest_schedules
    channel.metrics.event_executions = channel.event_executions()
    return ADMMResult(
        converged=converged,
        stop_reason=stop_reason,
        iterations=len(history),
        coordination_updates=coordination_updates,
        schedules=published_schedules,
        p_references_mw=references_p,
        q_references_mvar=references_q,
        history=history,
        communication=channel.metrics,
        aggregate_import_mw=np.sum(z_p, axis=0),
        aggregate_q_mvar=np.sum(z_q, axis=0),
        coordination_snapshot=snapshot,
        capabilities=capabilities,
    )

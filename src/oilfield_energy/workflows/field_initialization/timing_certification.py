"""Finite asynchronous ZOH/monotone reachability, independent of minute alpha.

Candidate commands come from the existing planner. Every timed path, endpoint,
deadline and terminal hold is re-proved over the full explicit forecast envelope.
No synchronous certificate is reused as a timed safety or task proof.
"""

from datetime import datetime, timedelta
from fractions import Fraction as F
from hashlib import sha256

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.kernel.intervals import Interval
from oilfield_energy.modules.control.api import (
    balance_policy_command,
    energy_return_command,
    validate_device_box,
)
from oilfield_energy.modules.control.contracts import TubeBox, TubeCommand, TubeState
from oilfield_energy.workflows.single_execution.api import (
    device_model_at,
    unified_network_box,
)
from oilfield_energy.workflows.single_execution.contracts import (
    UnifiedDomain,
    UnifiedPlanCertificate,
)

from .confirmed import confirmed_box, confirmed_snapshot, confirmed_step
from .contracts import InitializationEvidence, PccTargetEvidence
from .contracts_timing import BoundedTimingPolicy, TimedCycleProof
from .messages import canonical, policy_digest
from .observation_uncertainty import resource_settled
from .ports import ExecutionTransport
from .validation import require


def verify_timing_port(
    current: InitializationEvidence, transport: ExecutionTransport
) -> None:
    timing = current.request.payload.control_policy.bounded_timing
    observation = current.request.payload.control_policy.observation_uncertainty
    require(
        timing is None
        or timing.profile != "bounded_uncertain_v1"
        or transport.capabilities.timing_policy_sha256 == policy_digest(timing),
        "TIMING_POLICY_UNVERIFIED",
        "Transport did not attest the exact bounded-response and clock policy",
    )
    require(
        observation is None
        or transport.capabilities.observation_policy_sha256
        == policy_digest(observation),
        "OBSERVATION_POLICY_UNVERIFIED",
        "Transport did not attest the exact admitted metrology-policy content",
    )
    require(
        (
            timing is None
            and transport.capabilities.timing_profile == "software_synchronous_v1"
        )
        or (
            timing is not None
            and transport.capabilities.timing_profile == timing.profile
            and transport.capabilities.timing_revision == timing.revision
        ),
        "EXECUTION_TIMING_UNSUPPORTED",
        "Transport must implement the exact admitted timing profile and revision",
    )


def preparation_deadline(current: InitializationEvidence) -> datetime:
    timing = current.request.payload.control_policy.bounded_timing
    at = confirmed_snapshot(current).snapshot_at
    return at + timedelta(seconds=timing.prepare_max_seconds if timing else 0)


def midpoint(box: TubeBox) -> TubeState:
    def mid(i: Interval) -> float:
        return float((F(i.low) + F(i.high)) / 2)

    return TubeState(
        p_mw=tuple(mid(p) for p in box.p_mw),
        q_mvar=tuple(mid(q) for q in box.q_mvar),
        energy_mwh=mid(box.energy_mwh),
    )


def timed_transition(
    domain: UnifiedDomain,
    box: TubeBox,
    command: TubeCommand,
    step: int,
    timing: BoundedTimingPolicy,
) -> tuple[TubeBox, TubeBox]:
    """All independent application times in [earliest,latest], all monotone paths."""
    model = device_model_at(domain, step)
    validate_device_box(model, box)
    require(
        timing.profile == "bounded_uncertain_v1"
        or all(x.low == x.high for x in (*box.p_mw, *box.q_mvar)),
        "TIMED_RESPONSE_UNPROVEN",
        "Boundary P/Q must be settled at their held setpoints",
    )
    require(
        len(command.p_mw) == len(model.resources) == len(command.q_mvar),
        "IDENTITY_MISMATCH",
        "Timed command axes differ",
    )
    response = {r.resource_id: r for r in timing.resources}
    ranges = [
        resource_settled(response[r.resource_id], p, q)
        for r, p, q in zip(model.resources, command.p_mw, command.q_mvar, strict=True)
    ]
    q_movement = F(0)
    for resource, p, q, (end_p, end_q) in zip(
        model.resources, box.p_mw, box.q_mvar, ranges, strict=True
    ):
        require(
            max(abs(F(end_p.high) - F(p.low)), abs(F(end_p.low) - F(p.high)))
            <= F(resource.ramp_p_mw),
            "TIMED_RESPONSE_UNPROVEN",
            "Full asynchronous P movement exceeds the cycle budget",
        )
        q_movement += max(abs(F(end_q.high) - F(q.low)), abs(F(end_q.low) - F(q.high)))
    require(
        q_movement <= F(model.aggregate_q_ramp_mvar),
        "TIMED_RESPONSE_UNPROVEN",
        "Full asynchronous Q movement exceeds the cycle budget",
    )
    index = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    require(
        box.p_mw[index].low == box.p_mw[index].high
        and response[model.resources[index].resource_id].settled_p_error_mw == 0,
        "MODEL_UNSUPPORTED",
        "Storage active power must retain the independently verified ZOH setpoint",
    )

    def rate(power: float) -> F:
        p = F(power)
        return (
            -p / F(model.eta_discharge) if p >= 0 else -p * F(model.eta_charge)
        ) / 3600

    old, new = rate(box.p_mw[index].low), rate(command.p_mw[index])
    # Match datetime's microsecond wire clock exactly, including fractional seconds.
    clock_ticks = round(timing.clock_error_bound_seconds * 1_000_000)
    earliest = F(
        round(timing.apply_not_before_seconds * 1_000_000) - clock_ticks, 1_000_000
    )
    latest = F(
        round(timing.apply_not_after_seconds * 1_000_000) + clock_ticks, 1_000_000
    )
    changes = [old * t + new * (60 - t) for t in (earliest, latest)]
    energy = Interval(
        low=lower(F(box.energy_mwh.low) + min(changes)),
        high=upper(F(box.energy_mwh.high) + max(changes)),
    )
    path_changes = [F(0), *changes, old * earliest, old * latest]
    path_energy = Interval(
        low=lower(F(box.energy_mwh.low) + min(path_changes)),
        high=upper(F(box.energy_mwh.high) + max(path_changes)),
    )
    end = TubeBox(
        p_mw=tuple(r[0] for r in ranges),
        q_mvar=tuple(r[1] for r in ranges),
        energy_mwh=energy,
    )
    path = TubeBox(
        p_mw=tuple(
            Interval(low=min(x.low, u.low), high=max(x.high, u.high))
            for x, u in zip(box.p_mw, end.p_mw, strict=True)
        ),
        q_mvar=tuple(
            Interval(low=min(x.low, u.low), high=max(x.high, u.high))
            for x, u in zip(box.q_mvar, end.q_mvar, strict=True)
        ),
        energy_mwh=path_energy,
    )
    # Independent axes cover every device ordering; no P/Q correlation shortcut.
    validate_device_box(model, path)
    validate_device_box(device_model_at(domain, step + 1), end)
    return path, end


def candidate_command(
    domain: UnifiedDomain,
    plan: UnifiedPlanCertificate,
    state: TubeState,
    step: int,
) -> TubeCommand:
    """Proposal only. Robust certification below is the admission authority."""
    offset = step - plan.start_step
    command = (
        plan.commands[offset]
        if 0 <= offset < len(plan.commands)
        else domain.terminal_command
    )
    tracking = plan.tracking
    if tracking and 0 <= offset < len(tracking.policies):
        forecast = domain.forecasts[step]
        command = balance_policy_command(
            device_model_at(domain, step),
            state,
            tracking.policies[offset],
            sum((x.p_mw.low + x.p_mw.high) / 2 for x in forecast.loads),
            sum((x.q_mvar.low + x.q_mvar.high) / 2 for x in forecast.loads),
        )
    task = plan.task_continuation
    task_offset = step - task.recovery_start_step if task else -1
    if task and 0 <= task_offset < len(task.policies):
        command = energy_return_command(
            device_model_at(domain, step), state, task.policies[task_offset]
        )
    return command


def certify_timed_continuation(
    current: InitializationEvidence,
    target: PccTargetEvidence | None = None,
    *,
    retained: TimedCycleProof | None = None,
) -> TimedCycleProof:
    policy = current.request.payload.control_policy
    timing = policy.bounded_timing
    assert timing is not None
    domain, plan = (
        (target.domain, target.plan) if target else (current.domain, current.plan)
    )
    start = confirmed_step(current)
    origin = current.request.payload.planning_inputs.effective_from
    start_at = origin + timedelta(minutes=start)
    end = min(
        len(domain.forecasts) - 1,
        int((current.valid_until - origin).total_seconds() // 60),
    )
    require(
        start < end,
        "FORECAST_COVERAGE_INSUFFICIENT",
        "No complete timed minute remains",
    )
    box = confirmed_box(current)
    commands: list[TubeCommand] = []
    endpoints: list[TubeBox] = []
    digest = sha256(canonical(timing))
    if policy.observation_uncertainty:
        digest.update(canonical(policy.observation_uncertainty))
    first_path: TubeBox | None = None
    devices = {d.resource_id: d for d in current.resolved_model_package.devices}
    held = {h.resource_id: h for h in confirmed_snapshot(current).held_setpoints or ()}
    previous_command = (
        TubeCommand(
            p_mw=tuple(held[r.resource_id].p_mw for r in domain.devices.resources),
            q_mvar=tuple(held[r.resource_id].q_mvar for r in domain.devices.resources),
        )
        if held
        else None
    )
    task = current.request.payload.planning_inputs.energy_task
    task_proven = current.completed_energy_task or bool(
        task
        and task.deadline_at == start_at
        and task.energy_min_mwh
        <= box.energy_mwh.low
        <= box.energy_mwh.high
        <= task.energy_max_mwh
    )
    require(
        not task
        or not task.required
        or current.completed_energy_task
        or task.deadline_at > start_at
        or task.deadline_at == start_at
        and task.energy_min_mwh
        <= box.energy_mwh.low
        <= box.energy_mwh.high
        <= task.energy_max_mwh,
        "TIMED_TASK_UNPROVEN",
        "An elapsed task needs confirmed completion before timed admission",
    )
    for k in range(start, end):
        command = (
            retained.continuation_commands[k - retained.start_step]
            if retained
            and 0 <= k - retained.start_step < len(retained.continuation_commands)
            else candidate_command(domain, plan, midpoint(box), k)
        )
        for i, resource in enumerate(domain.devices.resources):
            d = devices[resource.resource_id]
            require(
                (
                    d.p_control_enabled
                    or command.p_mw[i]
                    == (
                        previous_command.p_mw[i]
                        if previous_command
                        else box.p_mw[i].low
                    )
                )
                and (
                    d.q_control_enabled
                    or command.q_mvar[i]
                    == (
                        previous_command.q_mvar[i]
                        if previous_command
                        else box.q_mvar[i].low
                    )
                ),
                "AUTHORITY_LOST",
                "A timed proposal changes an unauthorized axis",
            )
        path, after = timed_transition(domain, box, command, k, timing)
        if first_path is None:
            first_path = path
        for step, values in ((k, path), (k + 1, after)):
            network = unified_network_box(
                domain, values, domain.forecasts[step], reusable=True
            )
            require(network.certified, "TIMED_SAFETY_UNPROVEN", network.reason)
            at = origin + timedelta(minutes=step)
            if (
                target
                and target.request.payload.response_deadline_at
                <= at
                < target.guaranteed_until
            ):
                t = target.request.payload
                require(
                    network.pcc_p_mw is not None
                    and network.pcc_q_mvar is not None
                    and F(network.pcc_p_mw.low)
                    >= F(t.target_p_import_mw) - F(t.p_tolerance_mw)
                    and F(network.pcc_p_mw.high)
                    <= F(t.target_p_import_mw) + F(t.p_tolerance_mw)
                    and F(network.pcc_q_mvar.low)
                    >= F(t.target_q_import_mvar) - F(t.q_tolerance_mvar)
                    and F(network.pcc_q_mvar.high)
                    <= F(t.target_q_import_mvar) + F(t.q_tolerance_mvar),
                    "TIMED_TARGET_UNPROVEN",
                    "Asynchronous path cannot retain the original PCC guarantee",
                )
            digest.update(network.model_dump_json().encode())
        if task and not current.completed_energy_task:
            if origin + timedelta(minutes=k + 1) == task.deadline_at:
                task_proven = (
                    task.energy_min_mwh <= after.energy_mwh.low
                    and after.energy_mwh.high <= task.energy_max_mwh
                )
                require(
                    not task.required or task_proven,
                    "TIMED_TASK_UNPROVEN",
                    "All delayed storage outcomes must meet the original task",
                )
        commands.append(command)
        endpoints.append(after)
        box = after
        previous_command = command
        digest.update(
            path.model_dump_json().encode() + after.model_dump_json().encode()
        )
    require(
        task is None
        or not task.required
        or current.completed_energy_task
        or start_at <= task.deadline_at <= origin + timedelta(minutes=end),
        "TIMED_TASK_UNPROVEN",
        "Explicit timed coverage must include the original task deadline",
    )
    # Stop only at a certified terminal hold, never by silently extending forecasts.
    require(
        commands[-1] == domain.terminal_command,
        "TIMED_CONTINUATION_UNPROVEN",
        "Timed tail must reach the terminal hold within explicit coverage",
    )
    assert first_path is not None
    return TimedCycleProof(
        profile=timing.profile,
        clock_error_bound_seconds=timing.clock_error_bound_seconds,
        clock_revision=policy.observation_uncertainty.clock_revision
        if policy.observation_uncertainty
        else None,
        physical_apply_earliest_at=start_at
        + timedelta(
            seconds=timing.apply_not_before_seconds - timing.clock_error_bound_seconds
        ),
        physical_apply_latest_at=start_at
        + timedelta(
            seconds=timing.apply_not_after_seconds + timing.clock_error_bound_seconds
        ),
        energy_task_guarantee="not_required"
        if task is None
        else "maintained"
        if task_proven
        else "unproven",
        application_skew_max_seconds=timing.application_skew_max_seconds,
        profile_revision=timing.revision,
        start_step=start,
        apply_not_before_at=start_at
        + timedelta(seconds=timing.apply_not_before_seconds),
        apply_not_after_at=start_at + timedelta(seconds=timing.apply_not_after_seconds),
        prepare_deadline_at=start_at + timedelta(seconds=timing.prepare_max_seconds),
        dispatch_deadline_at=start_at
        + timedelta(
            seconds=timing.apply_not_before_seconds
            - timing.transport_max_seconds
            - timing.clock_error_bound_seconds
        ),
        path=first_path,
        endpoint=endpoints[0],
        continuation_commands=tuple(commands),
        continuation_endpoints=tuple(endpoints),
        guaranteed_until=origin + timedelta(minutes=end),
        proof_digest=digest.hexdigest(),
    )

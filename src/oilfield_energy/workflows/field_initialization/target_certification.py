"""Fixed external target proofs with explicit absolute clocks and expiry."""

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import acos, tan
from typing import Literal

from oilfield_energy.workflows.single_execution.api import certify_tracking_target

from .confirmed import (
    confirmed_box,
    confirmed_request,
    confirmed_snapshot,
    confirmed_step,
)
from .contracts import (
    InitializationEvidence,
    PccTarget,
    PccTargetEvidence,
    RequestPccTargetRequest,
)
from .contracts_values import ArtifactReference
from .timing_certification import certify_timed_continuation, preparation_deadline
from .validation import require, validate_snapshot


def target_identity(target: PccTarget) -> str:
    # Context may change on a renewal; all actual target obligations stay fixed.
    values = target.model_dump(mode="json", exclude={"context_id"})
    for key in ("effective_from", "response_deadline_at", "hold_until"):
        values[key] = getattr(target, key).astimezone(timezone.utc).isoformat()
    content = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(content.encode()).hexdigest()


def confirmed_boundary(
    current: InitializationEvidence, now: datetime, created_at: datetime
) -> None:
    require(
        current.field_run_state
        not in ("awaiting_feedback", "resync_required", "stopped"),
        "EXECUTION_NOT_READY",
        "A pending action, stop or resynchronization barrier prevents new actions",
    )
    require(
        current.observation_barrier is None,
        "RESYNC_REQUIRED",
        "New field observations prevent reliance on the old confirmed state; resynchronization is required",
    )
    at = confirmed_snapshot(current).snapshot_at
    if current.request.payload.control_policy.bounded_timing:
        require(
            now <= preparation_deadline(current),
            "CONFIRMED_STATE_REQUIRED",
            "Confirmed boundary missed the bounded preparation window",
        )
    require(
        at <= now < at + timedelta(minutes=1),
        "CONFIRMED_STATE_REQUIRED",
        "No confirmed device state for the current one-minute boundary",
    )
    require(
        now < current.valid_until,
        "CONTEXT_EXPIRED",
        "Active input evidence has expired",
    )
    mapping = current.resolved_model_package.point_mapping
    assert not isinstance(mapping, ArtifactReference)
    request = confirmed_request(current).model_copy(update={"created_at": created_at})
    validate_snapshot(
        request,
        current.domain.network.buses,
        current.domain.network.buses[current.domain.network.slack],
        mapping,
        now,
    )


def previous_strategy_status(
    current: InitializationEvidence | None,
    previous: PccTargetEvidence | None,
    now: datetime,
) -> Literal[
    "none", "conditional", "expired", "context_changed", "unconfirmed", "not_assessed"
]:
    if current is None:
        return "not_assessed"
    if previous is None:
        return "none"
    if current.domain.context_id != previous.context_id:
        return "context_changed"
    if now >= min(previous.guaranteed_until, current.valid_until):
        return "expired"
    try:
        confirmed_boundary(current, now, now)
    except ValueError:
        return "unconfirmed"
    return "conditional"


def certify_external_target(
    current: InitializationEvidence,
    request: RequestPccTargetRequest,
    *,
    minimum_guaranteed_until: datetime | None = None,
) -> PccTargetEvidence:
    target, base = request.payload, current.request.payload
    origin = base.planning_inputs.effective_from
    require(
        (
            target.effective_from == confirmed_snapshot(current).snapshot_at
            or minimum_guaranteed_until is not None
            and target.effective_from <= confirmed_snapshot(current).snapshot_at
        ),
        "CONFIRMED_STATE_REQUIRED",
        "Target needs a confirmed state at its effective boundary; no future queue exists",
    )
    for at in (target.effective_from, target.response_deadline_at, target.hold_until):
        require(
            (at - origin).total_seconds() % 60 == 0,
            "TARGET_TIME_UNALIGNED",
            "Target times must map exactly to the minute grid",
        )
    require(
        target.effective_from < target.response_deadline_at < target.hold_until,
        "TARGET_TIME_INVALID",
        "Target requires a future response deadline and at least one holding minute",
    )
    require(
        target.p_tolerance_mw <= base.control_policy.p_tracking_tolerance_mw
        and target.q_tolerance_mvar <= base.control_policy.q_tracking_tolerance_mvar,
        "TARGET_TOLERANCE_UNSUPPORTED",
        "Requested bands cannot loosen the configured tracking bands",
    )
    require(
        current.domain.network.pcc_min_mw
        <= target.target_p_import_mw
        <= current.domain.network.pcc_max_mw,
        "TARGET_OUTSIDE_LIMITS",
        "Requested active PCC target is outside the configured limits",
    )
    require(
        abs(target.target_q_import_mvar)
        <= current.domain.network.pcc_max_mw * tan(acos(current.domain.network.pf_min))
        + target.q_tolerance_mvar,
        "TARGET_OUTSIDE_LIMITS",
        "Reactive PCC target band cannot intersect the configured power-factor range",
    )
    # Expiry may be sub-minute (authority). Only complete certified minutes count.
    bound = min(
        target.hold_until,
        current.valid_until,
        base.planning_inputs.valid_until,
        base.authority.valid_until,
    )
    end = int((bound - origin).total_seconds() // 60)
    until = origin + timedelta(minutes=end)
    deadline = int((target.response_deadline_at - origin).total_seconds() // 60)
    require(
        end >= deadline + 1,
        "TARGET_COVERAGE_INSUFFICIENT",
        "Coverage must include the original response deadline and one full holding minute",
    )
    require(
        minimum_guaranteed_until is None or until >= minimum_guaranteed_until,
        "TARGET_GUARANTEE_WOULD_SHRINK",
        "Planning update would shorten the existing target guarantee",
    )
    domain = current.domain.model_copy(
        update={
            "p_band_mw": target.p_tolerance_mw,
            "q_band_mvar": target.q_tolerance_mvar,
        }
    )
    start = confirmed_step(current)
    offset = start - current.plan.start_step
    nominal = (
        current.plan.commands[offset]
        if 0 <= offset < len(current.plan.commands)
        else domain.terminal_command
    )
    task = base.planning_inputs.energy_task
    plan = certify_tracking_target(
        domain,
        confirmed_box(current),
        start,
        end,
        deadline,
        (target.target_p_import_mw, target.target_q_import_mvar),
        nominal,
        require_task=bool(task and task.required and not current.completed_energy_task),
    )
    tracking = plan.tracking
    require(
        tracking is not None, "TARGET_UNPROVEN", "Target tracking evidence is missing"
    )
    assert tracking is not None
    require(
        (tracking.target_p_mw, tracking.target_q_mvar)
        == (target.target_p_import_mw, target.target_q_import_mvar)
        and tracking.deadline_step == deadline
        and tracking.guaranteed_until_step == end,
        "TARGET_UNPROVEN",
        "Proof changed the fixed target or absolute clock",
    )
    require(
        start + len(plan.commands) <= len(domain.forecasts) - 1,
        "TARGET_COVERAGE_INSUFFICIENT",
        "Safe continuation exceeds explicit field forecast coverage",
    )
    evidence = PccTargetEvidence(
        request=request,
        context_id=domain.context_id,
        snapshot_id=confirmed_snapshot(current).snapshot_id,
        domain=domain,
        plan=plan,
        guaranteed_until=until,
    )
    if base.control_policy.bounded_timing:
        evidence = evidence.model_copy(
            update={"timed_proof": certify_timed_continuation(current, evidence)}
        )
    return evidence

"""Field lifecycle decisions and explicitly scoped status projections."""

from datetime import datetime, timedelta
from fractions import Fraction as F
from typing import Literal
from uuid import uuid4

from oilfield_energy.modules.control.api import point_box

from .confirmed import confirmed_box, confirmed_snapshot, confirmed_state
from .contracts import InitializationFailure
from .contracts_execution import (
    ExecutionPayload,
    ExecutionResponse,
    ExecutionStatus,
)
from .execution_context import ExecutionContext, ExecutionRequest
from .observation_uncertainty import observe_bounded


def status(ctx: ExecutionContext, now: datetime) -> ExecutionStatus:
    current, ledger, batch = ctx.current, ctx.ledger, ctx.batch
    if current is None:
        return ExecutionStatus(reason="Unknown run")
    task = current.request.payload.planning_inputs.energy_task
    snap = (
        ledger.confirmed.snapshot if ledger.confirmed else confirmed_snapshot(current)
    )
    actual = ledger.confirmed.state if ledger.confirmed else confirmed_state(current)
    outcome = "not_required" if task is None else ledger.energy_task_outcome
    plan = ctx.target.plan if ctx.target else current.plan
    task_proven = (
        plan.task_continuation is not None
        or current.completed_energy_task
        and ledger.energy_task_outcome == "met"
    )
    if current.request.payload.control_policy.bounded_timing:
        task_timing = (
            current.pending_cycle.timed_proof
            if current.pending_cycle
            else ctx.target.timed_proof
            if ctx.target
            else current.confirmed_boundary.timed_continuation
            if current.confirmed_boundary
            else current.timed_proof
        )
        task_proven = bool(
            task
            and ledger.energy_task_outcome == "met"
            and task.deadline_at <= snap.snapshot_at
        ) or bool(task_timing and task_timing.energy_task_guarantee == "maintained")
    target = ctx.target
    timed = batch.evidence.timed_proof if batch else None
    tracking: Literal["passed", "violated", "unproven", "not_assessed"] = "not_assessed"
    if (
        target
        and target.request.payload.response_deadline_at
        <= snap.snapshot_at
        < target.guaranteed_until
    ):
        t = target.request.payload
        assert (
            snap.pcc.p_import_mw.value is not None
            and snap.pcc.q_import_mvar.value is not None
        )
        tracking = (
            "passed"
            if abs(snap.pcc.p_import_mw.value - t.target_p_import_mw)
            <= t.p_tolerance_mw
            and abs(snap.pcc.q_import_mvar.value - t.target_q_import_mvar)
            <= t.q_tolerance_mvar
            else "violated"
        )
        if current.request.payload.control_policy.observation_uncertainty:
            values = observe_bounded(
                snap,
                current.resolved_model_package,
                current.request.payload.control_policy,
                tuple(r.resource_id for r in current.domain.devices.resources),
            )
            passed, disjoint = True, False
            for measured, goal, band in (
                (snap.pcc.p_import_mw, t.target_p_import_mw, t.p_tolerance_mw),
                (snap.pcc.q_import_mvar, t.target_q_import_mvar, t.q_tolerance_mvar),
            ):
                r = values.aligned_ranges[measured.source_ref]
                lo, hi = F(goal) - F(band), F(goal) + F(band)
                passed = passed and lo <= F(r.low) <= F(r.high) <= hi
                disjoint = disjoint or F(r.high) < lo or F(r.low) > hi
            tracking = "passed" if passed else "violated" if disjoint else "unproven"
    timing = current.request.payload.control_policy.bounded_timing
    return ExecutionStatus(
        representative_state=actual
        if current.request.payload.control_policy.observation_uncertainty
        else None,
        state_evidence_ref=ledger.confirmed.state_evidence_ref
        if ledger.confirmed
        else None,
        confirmed_state_box=(
            (ledger.confirmed.state_box or point_box(ledger.confirmed.state))
            if ledger.confirmed
            else confirmed_box(current)
        )
        if current.request.payload.control_policy.observation_uncertainty
        else None,
        state_semantics="bounded_set"
        if current.request.payload.control_policy.observation_uncertainty
        else "exact",
        timing_revision=timed.profile_revision if timed else None,
        apply_not_before_at=timed.apply_not_before_at if timed else None,
        apply_not_after_at=timed.apply_not_after_at if timed else None,
        dispatch_deadline_at=batch.dispatch_deadline_at if batch else None,
        feedback_due_at=batch.feedback_due_at if batch else None,
        validation_scope=timing.profile if timing else "software_synchronous_only",
        mode=current.request.payload.mode,
        context_id=current.domain.context_id,
        run_state=ledger.run_state,
        command_batch_id=batch.evidence.command_batch_id if batch else None,
        batch_status=batch.status if batch else None,
        cycle_status="confirmed"
        if batch and batch.status == "confirmed"
        else "unconfirmed"
        if batch and batch.status in ("unconfirmed", "cancelled", "reconciled")
        else "pending"
        if batch
        else None,
        command_results=batch.results if batch else (),
        pending_batch_id=ledger.pending_batch_id,
        confirmed_snapshot_id=snap.snapshot_id,
        last_confirmed_cycle_id=ledger.confirmed.last_cycle_id
        if ledger.confirmed
        else None,
        confirmed_state=None
        if current.request.payload.control_policy.observation_uncertainty
        else actual,
        stopped_at=ledger.stopped_at,
        target_id=target.request.payload.target_id if target else None,
        target_response_deadline_at=target.request.payload.response_deadline_at
        if target
        else None,
        target_guaranteed_until=target.guaranteed_until if target else None,
        energy_task_deadline_at=task.deadline_at if task else None,
        energy_task_guarantee="not_required"
        if task is None
        else "maintained"
        if task_proven
        and now < current.valid_until
        and ledger.run_state == "ready"
        and current.observation_barrier is None
        else "unproven",
        energy_task_outcome=outcome,
        tracking_status=tracking,
        evidence_ref=f"cycle:{batch.evidence.command_batch_id}" if batch else None,
        reason=ledger.reason or (batch.reason if batch else ""),
        resynchronization_id=ledger.last_resynchronization_id,
        recovery_expired_target_id=ledger.recovery_expired_target_id,
    )


def response(
    request: ExecutionRequest,
    ctx: ExecutionContext,
    now: datetime,
    failure: InitializationFailure | None = None,
    pending: bool = False,
) -> ExecutionResponse:
    return ExecutionResponse(
        operation=request.operation,
        message_id=str(uuid4()),
        correlation_id=request.message_id,
        microgrid_id=request.microgrid_id,
        run_id=request.run_id,
        created_at=now,
        payload=ExecutionPayload(
            outcome="rejected" if failure else "pending" if pending else "accepted",
            errors=(failure.error,) if failure else (),
            data=status(ctx, now),
        ),
    )


def block(ctx: ExecutionContext, reason: str) -> ExecutionContext:
    ledger = ctx.ledger.model_copy(
        update={
            "run_state": "stopped" if ctx.ledger.stopped_at else "resync_required",
            "reason": reason,
        }
    )
    batch = (
        ctx.batch.model_copy(update={"status": "unconfirmed", "reason": reason})
        if ctx.batch and ctx.batch.status != "confirmed"
        else ctx.batch
    )
    return ctx.model_copy(update={"ledger": ledger, "batch": batch})


def expire(ctx: ExecutionContext, now: datetime) -> ExecutionContext:
    if ctx.batch and ctx.ledger.pending_batch_id == ctx.batch.evidence.command_batch_id:
        if now > ctx.batch.feedback_due_at:
            return block(ctx, "FEEDBACK_TIMEOUT: execution remains unconfirmed")
        if ctx.batch.send_claimed_at is None and now > ctx.batch.dispatch_deadline_at:
            return block(ctx, "COMMAND_EXPIRED: unsent action missed its boundary")
        if ctx.batch.send_claimed_at is not None:
            assert ctx.current is not None
            timeout = (
                ctx.current.request.payload.control_policy.transport_timeout_seconds
            )
            timing = ctx.current.request.payload.control_policy.bounded_timing
            if timing:
                timeout = min(timeout, timing.transport_max_seconds)
            accepted = {
                r.command_id
                for r in ctx.batch.results
                if r.transport_status == "accepted"
            }
            if now > ctx.batch.send_claimed_at + timedelta(
                seconds=timeout
            ) and accepted != {c.command_id for c in ctx.batch.commands}:
                return block(ctx, "TRANSPORT_TIMEOUT: delivery remains unknown")
    if ctx.current and ctx.current.observation_barrier:
        return block(
            ctx, "RESYNC_REQUIRED: observation invalidated the retained conditions"
        )
    if (
        ctx.current
        and ctx.current.request.payload.mode == "closed_loop"
        and ctx.ledger.run_state == "ready"
    ):
        if now >= ctx.current.valid_until or now >= confirmed_snapshot(
            ctx.current
        ).snapshot_at + timedelta(minutes=1):
            return block(ctx, "CONFIRMED_STATE_REQUIRED: no fresh confirmed boundary")
    return ctx

"""Re-admit a measured boundary on the original absolute obligation clock."""

from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256

from oilfield_energy.modules.control.api import point_box
from oilfield_energy.workflows.single_execution.api import (
    certify_unified_plan,
    certify_unified_terminal,
)

from .confirmed import confirmed_snapshot
from .contracts import InitializationFailure, InitializeRequest, Snapshot
from .contracts_recovery import PrepareResynchronizationRequest
from .domain import map_planning_domain
from .execution_context import ExecutionContext
from .messages import canonical
from .observation_uncertainty import validate_clock
from .ports import ExecutionTransport, RecoveryVerifier
from .recovery_evidence import ResynchronizationEvidence
from .snapshot_assessment import assess_snapshot
from .snapshot_ordering import next_observation_cursor, sample_times
from .target_certification import certify_external_target
from .timing_certification import certify_timed_continuation, verify_timing_port
from .validation import require, validate_snapshot_values


def recovery_fingerprint(ctx: ExecutionContext) -> str:
    return sha256(
        b"|".join(
            canonical(x) if x is not None else b"null"
            for x in (ctx.current, ctx.ledger, ctx.batch, ctx.cursor, ctx.target)
        )
    ).hexdigest()


def verify_recovery_boundary(
    request: PrepareResynchronizationRequest,
    ctx: ExecutionContext,
    now: datetime,
    transport: ExecutionTransport | None,
    verifier: RecoveryVerifier | None,
) -> None:
    current, p = ctx.current, request.payload
    require(current is not None, "RUN_NOT_FOUND", "Unknown run")
    assert current is not None
    base = current.request.payload
    require(
        ctx.ledger.run_state in ("resync_required", "stopped"),
        "RECOVERY_NOT_REQUIRED",
        "Stop or a recorded resynchronization barrier is required before recovery",
    )
    require(
        p.context_id == current.domain.context_id == p.snapshot.context_id,
        "CONTEXT_CHANGED",
        "Recovery must bind the currently blocked context",
    )
    require(
        p.pending_batch_id == ctx.ledger.pending_batch_id,
        "PENDING_BATCH_CHANGED",
        "Pending action changed",
    )
    require(
        p.authority.valid_until > now
        and p.authority.permission_revision == base.authority.permission_revision,
        "AUTHORITY_LOST",
        "Recovery requires current authority for the unchanged permission model",
    )
    require(
        p.authority.epoch >= base.authority.epoch,
        "AUTHORITY_LOST",
        "Authority epoch cannot move backwards",
    )
    require(
        p.authority.owner_id == base.authority.owner_id
        or p.authority.epoch > base.authority.epoch,
        "AUTHORITY_LOST",
        "A new control owner requires a new authority epoch",
    )
    if base.mode == "closed_loop":
        require(
            transport is not None and p.authority.write_enabled,
            "TRANSPORT_UNAVAILABLE",
            "Recovery needs the configured software control interface",
        )
        assert transport is not None
        verify_timing_port(current, transport)
        require(
            transport.authorized(request.microgrid_id, p.authority),
            "AUTHORITY_LOST",
            "Recovery authority was not independently verified",
        )
    else:
        require(
            not p.authority.write_enabled,
            "AUTHORITY_LOST",
            "Shadow recovery cannot enable writes",
        )
    require(
        p.snapshot.operating_state_ref == base.initial_snapshot.operating_state_ref,
        "MODEL_UNSUPPORTED",
        "Recovery does not change topology, device capabilities or operating mode",
    )
    require(
        p.snapshot.snapshot_at >= confirmed_snapshot(current).snapshot_at
        and p.snapshot.snapshot_id != confirmed_snapshot(current).snapshot_id,
        "SNAPSHOT_OUT_OF_ORDER",
        "Recovery needs a new measured snapshot, never a predicted endpoint",
    )
    require(
        p.planning_inputs.effective_from == base.planning_inputs.effective_from,
        "RECOVERY_CLOCK_CHANGED",
        "Preserve the original planning clock and absolute task deadlines",
    )
    require(
        p.planning_inputs.energy_task == base.planning_inputs.energy_task,
        "TASK_CHANGE_NOT_SUPPORTED",
        "Recovery must preserve the original energy task in full",
    )
    require(
        base.planning_inputs.issued_at <= p.planning_inputs.issued_at <= now
        and p.planning_inputs.valid_until > p.snapshot.snapshot_at,
        "INPUT_EXPIRED_OR_FUTURE",
        "Recovery inputs are obsolete, expired or future-issued",
    )
    if p.planning_inputs.input_revision == base.planning_inputs.input_revision:
        require(
            p.planning_inputs == base.planning_inputs,
            "INPUT_REVISION_CONFLICT",
            "Existing input revision cannot change content",
        )
    else:
        require(
            p.planning_inputs.input_revision not in ctx.reserved_revisions
            or ctx.recovery is not None
            and ctx.recovery.request.payload == p,
            "INPUT_REVISION_CONFLICT",
            "Use an explicit new recovery input revision",
        )
    mapping = current.resolved_model_package.point_mapping
    assert isinstance(mapping, tuple)
    validate_snapshot_values(
        p.snapshot,
        current.resolved_model_package,
        base.control_policy,
        p.authority,
        current.domain.network.buses,
        current.domain.network.buses[current.domain.network.slack],
        mapping,
        now,
        request.created_at,
    )
    batch = ctx.batch
    expected_pending: frozenset[str] = (
        frozenset({batch.evidence.command_batch_id})
        if batch is not None and batch.send_claimed_at is not None
        else frozenset()
    )
    require(
        ctx.unsettled_batch_ids == expected_pending,
        "EXECUTION_NOT_SETTLED",
        "Unsettled execution history differs from the single pending action",
    )
    if batch is not None and batch.send_claimed_at is not None:
        q = p.quiescence
        require(
            q is not None and verifier is not None,
            "EXECUTION_NOT_SETTLED",
            "Claimed actions require verified terminal outcomes and a no-late-application fence",
        )
        assert q is not None and verifier is not None
        if base.control_policy.observation_uncertainty:
            fence_lo, fence_hi = validate_clock(
                q.timestamp_uncertainty, q.fenced_at, base.control_policy, now
            )
            earliest_sample = min(sample_times(p.snapshot).values())
            require(
                batch.send_claimed_at
                <= fence_lo
                <= fence_hi
                <= min(earliest_sample, p.snapshot.snapshot_at),
                "RECOVERY_FENCE_INVALID",
                "The complete fence-time range must precede every recovery sample",
            )
            for settlement in q.settlements:
                lo, hi = validate_clock(
                    settlement.timestamp_uncertainty,
                    settlement.terminal_at,
                    base.control_policy,
                    now,
                )
                require(
                    batch.send_claimed_at <= lo <= hi <= fence_lo,
                    "RECOVERY_FENCE_INVALID",
                    "A terminal-time range crosses the command fence",
                )
        else:
            require(
                q.timestamp_uncertainty is None
                and all(s.timestamp_uncertainty is None for s in q.settlements),
                "UNCERTAINTY_NOT_ADMITTED",
                "Exact recovery cannot ignore timestamp uncertainty",
            )
        require(
            q.command_batch_id == batch.evidence.command_batch_id
            and q.previous_authority == batch.evidence.request.payload.authority
            and p.authority.epoch > q.previous_authority.epoch,
            "RECOVERY_FENCE_INVALID",
            "The new control epoch must fence the original batch authority",
        )
        require(
            batch.send_claimed_at <= q.fenced_at <= p.snapshot.snapshot_at,
            "RECOVERY_FENCE_INVALID",
            "The new snapshot must follow the command fence",
        )
        expected = {c.command_id: c.resource_id for c in batch.commands}
        require(
            {s.command_id: s.resource_id for s in q.settlements} == expected,
            "EXECUTION_NOT_SETTLED",
            "Every original command needs one final disposition",
        )
        require(
            all(
                batch.send_claimed_at <= s.terminal_at <= q.fenced_at
                for s in q.settlements
            ),
            "RECOVERY_FENCE_INVALID",
            "Terminal action evidence must precede the fence",
        )
        known = {r.command_id: r.device_status for r in batch.results}
        require(
            all(
                (known.get(s.command_id) != "applied" or s.disposition == "applied")
                and (
                    known.get(s.command_id) != "rejected" or s.disposition != "applied"
                )
                for s in q.settlements
            ),
            "RECOVERY_SETTLEMENT_CONFLICT",
            "Settlement contradicts known device adoption facts",
        )
        try:
            verified = verifier.verify_quiescence(request.microgrid_id, p.authority, q)
        except Exception as exc:
            raise InitializationFailure(
                "RECOVERY_VERIFICATION_UNAVAILABLE",
                f"Closure verification failed ({type(exc).__name__})",
            ) from exc
        require(
            verified,
            "EXECUTION_NOT_SETTLED",
            "Closure and fencing provenance was not verified",
        )
    else:
        require(
            p.quiescence is None,
            "RECOVERY_FENCE_INVALID",
            "No claimed batch exists for the supplied quiescence evidence",
        )


def certify_resynchronization(
    request: PrepareResynchronizationRequest,
    ctx: ExecutionContext,
    clock: Callable[[], datetime],
    elapsed: Callable[[], float],
    started: float,
) -> ResynchronizationEvidence:
    current, p = ctx.current, request.payload
    assert current is not None
    base = current.request.payload
    task = p.planning_inputs.energy_task
    completed = bool(
        task
        and task.deadline_at < p.snapshot.snapshot_at
        and ctx.ledger.energy_task_outcome == "met"
    )
    require(
        not task or task.deadline_at >= p.snapshot.snapshot_at or completed,
        "TASK_DEADLINE_PASSED",
        "Recovery cannot manufacture a missing or missed past task outcome",
    )
    source = InitializeRequest.model_validate(
        current.request.model_dump()
        | {
            "message_id": request.message_id,
            "created_at": request.created_at,
            "payload": base.model_dump()
            | {
                "initial_snapshot": Snapshot.model_validate(
                    p.snapshot.model_dump() | {"context_id": None}
                ),
                "planning_inputs": p.planning_inputs,
                "authority": p.authority,
                "model_package": current.resolved_model_package,
            },
        }
    )
    context = sha256(
        canonical(request)
        + current.domain.network.model_dump_json().encode()
        + recovery_fingerprint(ctx).encode()
    ).hexdigest()
    numerical_plan = (
        p.planning_inputs.model_copy(update={"energy_task": None})
        if completed
        else p.planning_inputs
    )
    domain = map_planning_domain(
        current.resolved_model_package,
        numerical_plan,
        base.control_policy,
        current.domain.network,
        context,
    )
    provisional = current.model_copy(
        update={
            "request": source,
            "domain": domain,
            "valid_until": min(p.planning_inputs.valid_until, p.authority.valid_until),
            "observation_barrier": None,
            "confirmed_boundary": None,
            "initial_box": None,
            "field_run_state": None,
            "pending_cycle": None,
            "pending_sent": False,
            "completed_energy_task": completed,
        }
    )
    if base.control_policy.observation_uncertainty:
        provisional = provisional.model_copy(
            update={
                "valid_until": min(
                    provisional.valid_until,
                    base.control_policy.observation_uncertainty.valid_until,
                )
            }
        )
    assessment, errors = assess_snapshot(provisional, p.snapshot, None)
    if errors:
        raise InitializationFailure(errors[0].code, errors[0].message)
    actual = assessment.observed_state
    assert actual is not None
    proof = certify_unified_plan(
        domain,
        assessment.observed_box or point_box(actual),
        assessment.step,
        require_task=bool(task and task.required and not completed),
    )
    require(
        proof.start_step + len(proof.commands) < len(domain.forecasts),
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Recovery continuation exceeds explicit input coverage",
    )
    evidence = provisional.model_copy(
        update={
            "initial": actual,
            "initial_box": assessment.observed_box,
            "plan": proof,
            "terminal_proof_digest": certify_unified_terminal(domain),
        }
    )
    target, expired = None, None
    if ctx.target is not None:
        if ctx.target.request.payload.hold_until <= p.snapshot.snapshot_at:
            expired = ctx.target.request.payload.target_id
        else:
            target = certify_external_target(
                evidence,
                ctx.target.request,
                minimum_guaranteed_until=ctx.target.guaranteed_until,
            )
            _, errors = assess_snapshot(evidence, p.snapshot, target)
            if errors:
                raise InitializationFailure(errors[0].code, errors[0].message)
    if base.control_policy.bounded_timing:
        evidence = evidence.model_copy(
            update={"timed_proof": certify_timed_continuation(evidence, target)}
        )
    if ctx.cursor is not None and ctx.cursor.snapshot == p.snapshot:
        cursor = ctx.cursor
    else:
        cursor = next_observation_cursor(p.snapshot, base.initial_snapshot, ctx.cursor)
    cursor = cursor.model_copy(
        update={"snapshot": p.snapshot.model_copy(update={"context_id": context})}
    )
    dates = sample_times(p.snapshot).values()
    expires = min(
        p.snapshot.snapshot_at + timedelta(minutes=1),
        evidence.valid_until,
        *(
            at + timedelta(seconds=base.control_policy.telemetry_max_age_seconds)
            for at in dates
        ),
    )
    if base.control_policy.bounded_timing:
        expires = min(
            expires,
            p.snapshot.snapshot_at
            + timedelta(seconds=base.control_policy.bounded_timing.prepare_max_seconds),
        )
    require(
        clock() < expires
        and elapsed() - started <= base.control_policy.compute_budget_seconds,
        "COMPUTE_BUDGET_EXCEEDED",
        "Recovery evidence expired or exceeded its admission budget",
    )
    return ResynchronizationEvidence(
        request=request,
        base_fingerprint=recovery_fingerprint(ctx),
        inputs=evidence,
        target=target,
        expired_target_id=expired,
        cursor=cursor,
        expires_at=expires,
    )

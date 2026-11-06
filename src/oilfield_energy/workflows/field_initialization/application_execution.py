"""Archive command feedback and atomically confirm measured cycle endpoints."""

from collections.abc import Callable
from datetime import datetime
from time import monotonic

from .contracts import InitializationFailure
from .contracts_execution import (
    ConfirmedBoundary,
    ExecutionResponse,
    ReportExecutionRequest,
)
from .execution_context import ExecutionContext, ExecutionDecision
from .execution_rules import check_completion, merge_results, validate_report_energy
from .execution_state import block, expire, response
from .messages import canonical
from .observation_uncertainty import window
from .ports import ExecutionStore
from .validation import require


class ReportExecution:
    def __init__(
        self,
        store: ExecutionStore,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        self.store, self.clock, self.elapsed = store, clock, elapsed

    def execute(self, request: ReportExecutionRequest) -> ExecutionResponse:
        request = ReportExecutionRequest.model_validate_json(canonical(request))
        try:
            return self.store.apply(request, lambda ctx: self.decide(request, ctx))[0]
        except InitializationFailure as exc:
            return response(request, ExecutionContext(current=None), self.clock(), exc)

    def decide(
        self, request: ReportExecutionRequest, ctx: ExecutionContext
    ) -> ExecutionDecision:
        started = self.elapsed()
        now = self.clock()
        ctx = expire(ctx, now)
        p = request.payload
        identity_valid = False
        fence_violated = False
        historical = (
            ctx.batch is not None
            and ctx.ledger.pending_batch_id != ctx.batch.evidence.command_batch_id
        )
        try:
            require(
                ctx.current is not None and ctx.batch is not None,
                "BATCH_NOT_FOUND",
                "Unknown run or command batch",
            )
            assert ctx.current is not None and ctx.batch is not None
            current = ctx.current
            batch = ctx.batch
            e = batch.evidence
            require(
                p.context_id == e.domain.context_id
                and p.cycle_id == e.request.payload.cycle_id,
                "IDENTITY_MISMATCH",
                "Report does not identify the original batch context and cycle",
            )
            require(
                batch.send_claimed_at is not None,
                "COMMAND_NOT_SENT",
                "A prepared or shadow batch is not evidence of a sent action",
            )
            assert batch.send_claimed_at is not None
            identity_valid = True
            if batch.reconciled_at is not None:
                settlements = {s.command_id: s for s in batch.settlements}
                fence_violated = any(
                    r.device_status == "applied"
                    and (
                        r.applied_at is not None
                        and window(r.applied_at, r.timestamp_uncertainty)[1]
                        > window(
                            batch.reconciled_at,
                            batch.reconciliation_timestamp_uncertainty,
                        )[0]
                        or r.command_id in settlements
                        and settlements[r.command_id].disposition != "applied"
                    )
                    for r in p.command_results
                )
                require(
                    not fence_violated,
                    "RECOVERY_FENCE_VIOLATED",
                    "New execution evidence contradicts the recovery command fence",
                )
            require(
                p.reported_at <= request.created_at <= now,
                "INVALID_EXECUTION_TIME",
                "Feedback is from the future",
            )
            require(
                p.reported_at >= batch.send_claimed_at,
                "INVALID_EXECUTION_TIME",
                "Feedback predates dispatch",
            )
            results = merge_results(batch, p.command_results, p.reported_at)
            batch = batch.model_copy(update={"results": results})
            ctx = ctx.model_copy(update={"batch": batch})
            require(
                not any(
                    r.transport_status == "rejected" or r.device_status == "rejected"
                    for r in results
                )
                and p.feedback_stage != "exception",
                "PARTIAL_OR_UNKNOWN_EXECUTION",
                p.reason or "A device rejected or overrode a command",
            )
            if p.feedback_stage == "cycle_end":
                if batch.status == "confirmed":
                    require(
                        batch.confirmed_snapshot_id == p.observed_snapshot_id,
                        "EXECUTION_RESULT_CONFLICT",
                        "Cycle already confirmed using a different snapshot",
                    )
                    if p.energy_evidence is not None:
                        require(
                            ctx.snapshot is not None,
                            "SNAPSHOT_NOT_CURRENT",
                            "New historical energy evidence still needs its immutable sample",
                        )
                        assert ctx.snapshot is not None
                        validate_report_energy(
                            p,
                            batch,
                            ctx.snapshot,
                            uncertain=e.timed_proof is not None
                            and e.timed_proof.profile == "bounded_uncertain_v1",
                        )
                    # Archived completion cannot advance a newer boundary or
                    # become invalid merely because it was replayed after expiry.
                elif historical:
                    require(
                        False,
                        "CYCLE_OUT_OF_ORDER",
                        "Historical feedback is archived without advancing current state",
                    )
                else:
                    require(
                        ctx.ledger.run_state != "resync_required"
                        and current.observation_barrier is None,
                        "RESYNC_REQUIRED",
                        "Recorded execution facts cannot release a resynchronization barrier",
                    )
                    require(
                        ctx.ledger.pending_batch_id == e.command_batch_id,
                        "CYCLE_OUT_OF_ORDER",
                        "Only the single pending cycle may advance state",
                    )
                    actual, state_box = check_completion(ctx, p, batch, now)
                    require(
                        self.clock() <= batch.feedback_due_at
                        and self.elapsed() - started
                        <= current.request.payload.control_policy.compute_budget_seconds,
                        "COMPUTE_BUDGET_EXCEEDED",
                        "Confirmation expired during model validation",
                    )
                    assert ctx.snapshot is not None
                    boundary = ConfirmedBoundary(
                        state_box=state_box,
                        state_evidence_ref=f"physical-reachability-and-measurement:{e.command_batch_id}"
                        if state_box
                        else None,
                        timed_continuation=e.timed_proof,
                        snapshot=ctx.snapshot,
                        state=actual,
                        expected=e.prepared.expected_next,
                        last_cycle_id=p.cycle_id,
                    )
                    ledger = ctx.ledger.model_copy(
                        update={
                            "confirmed": boundary,
                            "pending_batch_id": None,
                            "run_state": "stopped"
                            if ctx.ledger.stopped_at
                            else "ready",
                        }
                    )
                    task = current.request.payload.planning_inputs.energy_task
                    if task and boundary.snapshot.snapshot_at == task.deadline_at:
                        low = (
                            state_box.energy_mwh.low if state_box else actual.energy_mwh
                        )
                        high = (
                            state_box.energy_mwh.high
                            if state_box
                            else actual.energy_mwh
                        )
                        outcome = (
                            "met"
                            if task.energy_min_mwh <= low <= high <= task.energy_max_mwh
                            else "missed"
                            if high < task.energy_min_mwh or low > task.energy_max_mwh
                            else "indeterminate"
                        )
                        ledger = ledger.model_copy(
                            update={"energy_task_outcome": outcome}
                        )
                    batch = batch.model_copy(
                        update={
                            "status": "confirmed",
                            "confirmed_snapshot_id": p.observed_snapshot_id,
                        }
                    )
                    ctx = ctx.model_copy(update={"ledger": ledger, "batch": batch})
            elif batch.status == "sending":
                ctx = ctx.model_copy(
                    update={"batch": batch.model_copy(update={"status": "sent"})}
                )
            failure = None
        except InitializationFailure as exc:
            failure = exc
            if identity_valid and (not historical or fence_violated):
                ctx = block(ctx, f"{exc.error.code}: {exc}")
        return ExecutionDecision(
            response=response(
                request,
                ctx,
                now,
                failure,
                pending=not failure
                and ctx.batch is not None
                and ctx.batch.status
                not in ("confirmed", "unconfirmed", "cancelled", "reconciled"),
            ),
            ledger=ctx.ledger,
            batch=ctx.batch,
        )

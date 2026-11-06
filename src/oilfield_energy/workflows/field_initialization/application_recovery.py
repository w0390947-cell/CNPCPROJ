"""Two explicit recovery operations; preparation never releases a control barrier."""

from collections.abc import Callable
from datetime import datetime
from time import monotonic

from oilfield_energy.modules.control.api import point_box

from .contracts import InitializationFailure
from .contracts_execution import (
    ConfirmedBoundary,
    ExecutionResponse,
    RecoverySettlement,
)
from .contracts_recovery import PrepareResynchronizationRequest, ResumeRunRequest
from .execution_context import ExecutionContext, ExecutionDecision
from .execution_state import expire, response
from .messages import canonical
from .ports import ExecutionStore, ExecutionTransport, RecoveryVerifier
from .recovery_certification import (
    certify_resynchronization,
    recovery_fingerprint,
    verify_recovery_boundary,
)
from .recovery_evidence import ResynchronizationEvidence
from .timing_certification import preparation_deadline
from .validation import require


def recovery_response(
    request: PrepareResynchronizationRequest | ResumeRunRequest,
    ctx: ExecutionContext,
    now: datetime,
    evidence: ResynchronizationEvidence | None = None,
    failure: InitializationFailure | None = None,
    resumed: bool = False,
) -> ExecutionResponse:
    result = response(
        request,
        ctx,
        now,
        failure,
        pending=evidence is not None and not resumed and failure is None,
    )
    data = result.payload.data.model_copy(
        update={
            "resynchronization_id": getattr(
                request.payload, "resynchronization_id", None
            ),
            "proposed_context_id": evidence.inputs.domain.context_id
            if evidence
            else None,
            "recovery_status": "rejected"
            if failure
            else "resumed"
            if resumed
            else "prepared",
            "recovery_expires_at": evidence.expires_at if evidence else None,
            "evidence_ref": f"resynchronization:{request.run_id}:{request.payload.resynchronization_id}",
        }
    )
    return result.model_copy(
        update={"payload": result.payload.model_copy(update={"data": data})}
    )


class PrepareResynchronization:
    def __init__(
        self,
        store: ExecutionStore,
        clock: Callable[[], datetime],
        transport: ExecutionTransport | None = None,
        verifier: RecoveryVerifier | None = None,
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        self.store, self.clock, self.transport, self.verifier, self.elapsed = (
            store,
            clock,
            transport,
            verifier,
            elapsed,
        )

    def execute(self, request: PrepareResynchronizationRequest) -> ExecutionResponse:
        request = PrepareResynchronizationRequest.model_validate_json(
            canonical(request)
        )
        try:
            return self.store.apply(request, lambda ctx: self.decide(request, ctx))[0]
        except InitializationFailure as exc:
            return recovery_response(
                request, ExecutionContext(current=None), self.clock(), failure=exc
            )

    def decide(
        self, request: PrepareResynchronizationRequest, ctx: ExecutionContext
    ) -> ExecutionDecision:
        ctx = expire(ctx, self.clock())
        started = self.elapsed()
        try:
            if ctx.recovery is not None:
                require(
                    ctx.recovery.request.payload == request.payload,
                    "RECOVERY_ID_CONFLICT",
                    "Recovery ID is bound to an immutable proposal",
                )
                require(
                    not ctx.recovery_consumed,
                    "RECOVERY_ALREADY_CONSUMED",
                    "Recovery was already resumed",
                )
                return ExecutionDecision(
                    response=recovery_response(
                        request, ctx, self.clock(), ctx.recovery
                    ),
                    ledger=ctx.ledger,
                    batch=ctx.batch,
                )
            verify_recovery_boundary(
                request, ctx, self.clock(), self.transport, self.verifier
            )
            candidate = certify_resynchronization(
                request, ctx, self.clock, self.elapsed, started
            )
            verify_recovery_boundary(
                request, ctx, self.clock(), self.transport, self.verifier
            )
            assert ctx.current is not None
            require(
                self.clock() < candidate.expires_at
                and self.elapsed() - started
                <= ctx.current.request.payload.control_policy.compute_budget_seconds,
                "COMPUTE_BUDGET_EXCEEDED",
                "Recovery admission exceeded its calculation budget",
            )
            return ExecutionDecision(
                response=recovery_response(request, ctx, self.clock(), candidate),
                ledger=ctx.ledger,
                batch=ctx.batch,
                recovery=candidate,
            )
        except (ValueError, ArithmeticError) as exc:
            failure = (
                exc
                if isinstance(exc, InitializationFailure)
                else InitializationFailure("RECOVERY_UNPROVEN", str(exc))
            )
            return ExecutionDecision(
                response=recovery_response(request, ctx, self.clock(), failure=failure),
                ledger=ctx.ledger,
                batch=ctx.batch,
            )


class ResumeRun:
    def __init__(
        self,
        store: ExecutionStore,
        clock: Callable[[], datetime],
        transport: ExecutionTransport | None = None,
        verifier: RecoveryVerifier | None = None,
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        self.store, self.clock, self.transport, self.verifier, self.elapsed = (
            store,
            clock,
            transport,
            verifier,
            elapsed,
        )

    def execute(self, request: ResumeRunRequest) -> ExecutionResponse:
        request = ResumeRunRequest.model_validate_json(canonical(request))
        try:
            return self.store.apply(request, lambda ctx: self.decide(request, ctx))[0]
        except InitializationFailure as exc:
            return recovery_response(
                request, ExecutionContext(current=None), self.clock(), failure=exc
            )

    def decide(
        self, request: ResumeRunRequest, ctx: ExecutionContext
    ) -> ExecutionDecision:
        ctx = expire(ctx, self.clock())
        started = self.elapsed()
        try:
            candidate = ctx.recovery
            require(
                candidate is not None,
                "RECOVERY_NOT_FOUND",
                "Prepare a recovery candidate before resuming",
            )
            assert candidate is not None
            require(
                0
                <= (self.clock() - request.created_at).total_seconds()
                <= candidate.inputs.request.payload.control_policy.telemetry_max_age_seconds,
                "INVALID_MESSAGE_TIME",
                "Resume request is stale or future-dated",
            )
            p, proposal = request.payload, candidate.request.payload
            require(
                not ctx.recovery_consumed,
                "RECOVERY_ALREADY_CONSUMED",
                "Recovery can only be committed once",
            )
            require(
                p.context_id == proposal.context_id
                and p.proposed_context_id == candidate.inputs.domain.context_id
                and p.authority == proposal.authority,
                "CONTEXT_CHANGED",
                "Resume must name the prepared context and verified authority",
            )
            require(
                self.clock() < candidate.expires_at,
                "RECOVERY_EXPIRED",
                "Prepare a fresh recovery snapshot",
            )
            require(
                recovery_fingerprint(ctx) == candidate.base_fingerprint,
                "RECOVERY_STALE",
                "New observations, execution facts or control decisions invalidate this candidate",
            )
            verify_recovery_boundary(
                candidate.request, ctx, self.clock(), self.transport, self.verifier
            )
            checked = certify_resynchronization(
                candidate.request, ctx, self.clock, self.elapsed, started
            )
            require(
                checked == candidate,
                "RECOVERY_STALE",
                "Recovery evidence no longer recomputes",
            )
            if candidate.inputs.request.payload.mode == "closed_loop":
                require(
                    proposal.snapshot.snapshot_at
                    <= self.clock()
                    <= preparation_deadline(candidate.inputs),
                    "EXECUTION_TIMING_UNSUPPORTED",
                    "Recovery must commit inside its admitted preparation window",
                )
            require(
                self.clock() < candidate.expires_at
                and self.elapsed() - started
                <= candidate.inputs.request.payload.control_policy.compute_budget_seconds,
                "COMPUTE_BUDGET_EXCEEDED",
                "Recovery expired during final validation",
            )
            verify_recovery_boundary(
                candidate.request, ctx, self.clock(), self.transport, self.verifier
            )
            require(
                self.clock() < candidate.expires_at
                and self.elapsed() - started
                <= candidate.inputs.request.payload.control_policy.compute_budget_seconds
                and (
                    candidate.inputs.request.payload.mode != "closed_loop"
                    or proposal.snapshot.snapshot_at
                    <= self.clock()
                    <= preparation_deadline(candidate.inputs)
                ),
                "RECOVERY_EXPIRED",
                "Recovery conditions expired during authority verification",
            )
            last_cycle = (
                ctx.ledger.confirmed.last_cycle_id if ctx.ledger.confirmed else None
            )
            boundary = ConfirmedBoundary(
                state_box=candidate.inputs.initial_box,
                state_evidence_ref=f"resynchronization:{request.run_id}:{proposal.resynchronization_id}",
                timed_continuation=candidate.inputs.timed_proof,
                snapshot=candidate.cursor.snapshot,
                state=candidate.inputs.initial,
                expected=point_box(candidate.inputs.initial),
                last_cycle_id=last_cycle,
            )
            ledger = ctx.ledger.model_copy(
                update={
                    "confirmed": boundary,
                    "run_state": "ready",
                    "pending_batch_id": None,
                    "stopped_at": None,
                    "reason": "Explicit resynchronization committed; historical execution assessments unchanged",
                    "last_resynchronization_id": proposal.resynchronization_id,
                    "control_revision": ctx.ledger.control_revision + 1,
                    "recovery_expired_target_id": candidate.expired_target_id
                    or ctx.ledger.recovery_expired_target_id,
                }
            )
            batch = ctx.batch
            if batch:
                settlements = (
                    proposal.quiescence.settlements
                    if proposal.quiescence
                    else tuple(
                        RecoverySettlement(
                            command_id=c.command_id,
                            resource_id=c.resource_id,
                            disposition="not_sent",
                            terminal_at=proposal.snapshot.snapshot_at,
                            source_ref="system:unclaimed-batch-cancelled",
                        )
                        for c in batch.commands
                    )
                )
                batch = batch.model_copy(
                    update={
                        "status": "reconciled",
                        "settlements": settlements,
                        "reconciled_at": proposal.quiescence.fenced_at
                        if proposal.quiescence
                        else proposal.snapshot.snapshot_at,
                        "reconciliation_timestamp_uncertainty": proposal.quiescence.timestamp_uncertainty
                        if proposal.quiescence
                        else None,
                        "reason": "Settled for explicit recovery; no retrospective execution or safety success is claimed",
                    }
                )
            result_ctx = ctx.model_copy(
                update={
                    "current": candidate.inputs,
                    "target": candidate.target,
                    "ledger": ledger,
                    "batch": batch,
                    "cursor": candidate.cursor,
                }
            )
            return ExecutionDecision(
                response=recovery_response(
                    request, result_ctx, self.clock(), candidate, resumed=True
                ),
                ledger=ledger,
                batch=batch,
                recovery=candidate,
                activate_recovery=True,
            )
        except (ValueError, ArithmeticError) as exc:
            failure = (
                exc
                if isinstance(exc, InitializationFailure)
                else InitializationFailure("RECOVERY_UNPROVEN", str(exc))
            )
            return ExecutionDecision(
                response=recovery_response(request, ctx, self.clock(), failure=failure),
                ledger=ctx.ledger,
                batch=ctx.batch,
            )

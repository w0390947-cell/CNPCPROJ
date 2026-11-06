"""Persist a send claim before calling the injected transport, never auto-resend."""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from .application_execution import ReportExecution
from .contracts import InitializationFailure
from .contracts_execution import (
    CommandResult,
    DispatchControlCycleRequest,
    ExecutionBatch,
    ExecutionReport,
    ExecutionResponse,
    ReportExecutionRequest,
)
from .execution_context import ExecutionContext, ExecutionDecision
from .execution_state import expire, response
from .messages import canonical
from .ports import ExecutionStore, ExecutionTransport
from .timing_certification import verify_timing_port
from .validation import require


class DispatchControlCycle:
    def __init__(
        self,
        store: ExecutionStore,
        clock: Callable[[], datetime],
        transport: ExecutionTransport | None = None,
    ) -> None:
        self.store, self.clock, self.transport = store, clock, transport

    def execute(self, request: DispatchControlCycleRequest) -> ExecutionResponse:
        request = DispatchControlCycleRequest.model_validate_json(canonical(request))
        claimed: ExecutionBatch | None = None

        def claim(ctx: ExecutionContext) -> ExecutionDecision:
            nonlocal claimed
            ctx = expire(ctx, self.clock())
            failure = None
            try:
                require(
                    ctx.current is not None and ctx.batch is not None,
                    "BATCH_NOT_FOUND",
                    "Unknown execution batch",
                )
                assert ctx.current is not None and ctx.batch is not None
                base, batch = ctx.current.request.payload, ctx.batch
                require(
                    base.mode == "closed_loop" and self.transport is not None,
                    "CLOSED_LOOP_UNAVAILABLE",
                    "No trusted execution transport",
                )
                assert self.transport is not None
                verify_timing_port(ctx.current, self.transport)
                require(
                    request.payload.context_id
                    == batch.evidence.domain.context_id
                    == ctx.current.domain.context_id,
                    "CONTEXT_CHANGED",
                    "Dispatch context changed",
                )
                require(
                    ctx.ledger.run_state == "awaiting_feedback"
                    and batch.status == "prepared"
                    and ctx.ledger.pending_batch_id == batch.evidence.command_batch_id
                    and ctx.current.observation_barrier is None,
                    "COMMAND_WITHHELD",
                    "Batch is not the current unsent action",
                )
                require(
                    request.payload.authority == base.authority
                    and base.authority.valid_until > self.clock()
                    and self.transport.authorized(request.microgrid_id, base.authority),
                    "AUTHORITY_LOST",
                    "Authority was not verified by the injected transport",
                )
                require(
                    request.created_at <= self.clock()
                    and (
                        batch.evidence.request.payload.cycle_start_at
                        <= self.clock()
                        <= batch.dispatch_deadline_at
                    ),
                    "COMMAND_EXPIRED",
                    "Synchronous software send must occur exactly at the certified boundary",
                )
                batch = batch.model_copy(
                    update={"status": "sending", "send_claimed_at": self.clock()}
                )
                ctx = ctx.model_copy(update={"batch": batch})
                claimed = batch
            except InitializationFailure as exc:
                failure = exc
            return ExecutionDecision(
                response=response(request, ctx, self.clock(), failure, pending=not failure),
                ledger=ctx.ledger,
                batch=ctx.batch,
            )

        try:
            result, replayed = self.store.apply(request, claim)
        except InitializationFailure as exc:
            return response(request, ExecutionContext(current=None), self.clock(), exc)
        if replayed or claimed is None:
            return result
        assert self.transport is not None
        # A committed send claim is an in-flight action. A crash here must be
        # reconciled via query, never by replaying send. Stop preserves this claim.
        reason = ""
        results: tuple[CommandResult, ...] = ()
        try:
            latest = self.store.inspect(request)
            require(
                latest.current is not None
                and latest.ledger.run_state == "awaiting_feedback"
                and latest.current.observation_barrier is None,
                "COMMAND_WITHHELD",
                "Run was stopped or invalidated before transport invocation",
            )
            assert latest.current is not None
            verify_timing_port(latest.current, self.transport)
            require(
                self.transport.authorized(request.microgrid_id, request.payload.authority),
                "AUTHORITY_LOST",
                "Authority changed after the send claim",
            )
            require(
                claimed.evidence.request.payload.cycle_start_at
                <= self.clock()
                <= claimed.dispatch_deadline_at,
                "COMMAND_EXPIRED",
                "Boundary elapsed before transport invocation",
            )
            results = tuple(
                CommandResult.model_validate_json(canonical(x))
                for x in self.transport.send(request.microgrid_id, claimed)
            )
            timing = latest.current.request.payload.control_policy.bounded_timing
            require(
                timing is None
                or (
                    claimed.send_claimed_at is not None
                    and (self.clock() - claimed.send_claimed_at).total_seconds()
                    <= timing.transport_max_seconds
                ),
                "TRANSPORT_TIMEOUT",
                "Send exceeded the declared transport bound; reconcile without resend",
            )
        except Exception as exc:
            reason = f"Transport invocation failed ({type(exc).__name__}); execution requires reconciliation"
        recorded_at = self.clock()
        feedback = ReportExecutionRequest(
            interface_version=request.interface_version,
            operation="ReportExecution",
            message_id=f"dispatch-result:{uuid4()}",
            correlation_id=request.message_id,
            microgrid_id=request.microgrid_id,
            run_id=request.run_id,
            created_at=recorded_at,
            payload=ExecutionReport(
                context_id=claimed.evidence.domain.context_id,
                cycle_id=claimed.evidence.request.payload.cycle_id,
                command_batch_id=claimed.evidence.command_batch_id,
                feedback_stage="exception" if reason else "device_ack",
                reported_at=recorded_at,
                command_results=results,
                observed_snapshot_id=None,
                energy_evidence=None,
                reason=reason,
            ),
        )
        ReportExecution(self.store, self.clock).execute(feedback)
        # This immutable response acknowledges the send claim, not execution.
        return result

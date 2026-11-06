"""Reconcile unknown sends, query durable facts and stop future control actions."""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from .application_execution import ReportExecution
from .contracts import InitializationFailure
from .contracts_execution import (
    CommandResult,
    ExecutionResponse,
    QueryRunStatusRequest,
    ReportExecutionRequest,
    StopRunRequest,
)
from .execution_context import ExecutionContext, ExecutionDecision
from .execution_state import expire, response
from .messages import canonical
from .ports import ExecutionStore, ExecutionTransport
from .validation import require


class QueryRunStatus:
    def __init__(
        self,
        store: ExecutionStore,
        clock: Callable[[], datetime],
        transport: ExecutionTransport | None = None,
    ) -> None:
        self.store, self.clock, self.transport = store, clock, transport

    def execute(self, request: QueryRunStatusRequest) -> ExecutionResponse:
        request = QueryRunStatusRequest.model_validate_json(canonical(request))
        try:
            cached = self.store.recorded(request)
            if cached is not None:
                return cached
            if request.payload.reconcile:
                view = self.store.inspect(request)
                if view.batch and view.batch.send_claimed_at:
                    require(
                        self.transport is not None,
                        "TRANSPORT_UNAVAILABLE",
                        "No execution query port is configured",
                    )
                    assert self.transport is not None
                    # Network I/O occurs outside the session transaction. Results
                    # remain bound to the captured immutable batch, even if the
                    # current head changes while the query is in flight.
                    try:
                        results = tuple(
                            CommandResult.model_validate_json(canonical(x))
                            for x in self.transport.query(request.microgrid_id, view.batch)
                        )
                    except Exception as exc:
                        failure = InitializationFailure(
                            "TRANSPORT_UNAVAILABLE",
                            f"Transport query failed ({type(exc).__name__})",
                        )

                        def failed(ctx: ExecutionContext) -> ExecutionDecision:
                            ctx = expire(ctx, self.clock())
                            return ExecutionDecision(
                                response=response(request, ctx, self.clock(), failure),
                                ledger=ctx.ledger,
                                batch=ctx.batch,
                            )

                        return self.store.apply(request, failed)[0]
                    recorded_at = self.clock()
                    report = ReportExecutionRequest.model_validate(
                        request.model_dump()
                        | {
                            "operation": "ReportExecution",
                            "created_at": recorded_at,
                            "message_id": f"reconcile:{uuid4()}",
                            "payload": {
                                "context_id": view.batch.evidence.domain.context_id,
                                "cycle_id": view.batch.evidence.request.payload.cycle_id,
                                "command_batch_id": view.batch.evidence.command_batch_id,
                                "feedback_stage": "transport",
                                "reported_at": recorded_at,
                                "command_results": results,
                                "observed_snapshot_id": None,
                                "energy_evidence": None,
                                "reason": "Transport query reconciliation",
                            },
                        }
                    )
                    ReportExecution(self.store, self.clock).execute(report)
            return self.store.apply(request, lambda ctx: self.decide(request, ctx))[0]
        except InitializationFailure as exc:
            return response(request, ExecutionContext(current=None), self.clock(), exc)

    def decide(self, request: QueryRunStatusRequest, ctx: ExecutionContext) -> ExecutionDecision:
        ctx = expire(ctx, self.clock())
        failure = None
        try:
            require(ctx.current is not None, "RUN_NOT_FOUND", "Unknown run")
            require(
                request.payload.command_batch_id is None or ctx.batch is not None,
                "BATCH_NOT_FOUND",
                "Unknown batch",
            )
        except InitializationFailure as exc:
            failure = exc
        except (OSError, TimeoutError) as exc:
            failure = InitializationFailure("TRANSPORT_UNAVAILABLE", str(exc))
        return ExecutionDecision(
            response=response(request, ctx, self.clock(), failure),
            ledger=ctx.ledger,
            batch=ctx.batch,
        )


class StopRun:
    def __init__(
        self,
        store: ExecutionStore,
        clock: Callable[[], datetime],
        transport: ExecutionTransport | None = None,
    ) -> None:
        self.store, self.clock, self.transport = store, clock, transport

    def execute(self, request: StopRunRequest) -> ExecutionResponse:
        request = StopRunRequest.model_validate_json(canonical(request))
        try:
            return self.store.apply(request, lambda ctx: self.decide(request, ctx))[0]
        except InitializationFailure as exc:
            return response(request, ExecutionContext(current=None), self.clock(), exc)

    def decide(self, request: StopRunRequest, ctx: ExecutionContext) -> ExecutionDecision:
        failure = None
        try:
            require(ctx.current is not None, "RUN_NOT_FOUND", "Unknown run")
            assert ctx.current is not None
            p, base = request.payload, ctx.current.request.payload
            require(
                p.requested_at <= self.clock() and request.created_at <= self.clock(),
                "INVALID_MESSAGE_TIME",
                "Stop request is from the future",
            )
            require(
                p.authority.valid_until > self.clock(),
                "AUTHORITY_LOST",
                "Stop requires current verified management authority",
            )
            require(
                (base.mode == "shadow" and p.authority == base.authority)
                or (
                    self.transport is not None
                    and self.transport.authorized(request.microgrid_id, p.authority)
                ),
                "AUTHORITY_LOST",
                "Stop authority could not be verified",
            )
            ledger = ctx.ledger.model_copy(
                update={
                    "run_state": "stopped",
                    "stopped_at": ctx.ledger.stopped_at or self.clock(),
                    "reason": p.reason,
                    "control_revision": ctx.ledger.control_revision + 1,
                }
            )
            batch = ctx.batch
            if batch and batch.status == "prepared":
                batch = batch.model_copy(
                    update={"status": "cancelled", "reason": "Stopped before dispatch"}
                )
                ledger = ledger.model_copy(update={"pending_batch_id": None})
            ctx = ctx.model_copy(update={"ledger": ledger, "batch": batch})
        except InitializationFailure as exc:
            failure = exc
        return ExecutionDecision(
            response=response(request, ctx, self.clock(), failure),
            ledger=ctx.ledger,
            batch=ctx.batch,
        )

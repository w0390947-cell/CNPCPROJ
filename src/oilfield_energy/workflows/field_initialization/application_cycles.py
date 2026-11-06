"""Prepare immutable certified batches without committing device execution."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from .contracts import (
    InitializationEvidence,
    InitializationFailure,
    ObservationCursor,
    PccTargetEvidence,
)
from .contracts_cycles import (
    ControlCycleEvidence,
    ControlCycleResponsePayload,
    ControlCycleResult,
    CycleModelCheck,
    DeviceCommand,
    PredictedDeviceState,
    PredictedEndpoint,
    PrepareControlCycleRequest,
    PrepareControlCycleResponse,
)
from .cycle_preparation import prepare_cycle_evidence
from .messages import canonical
from .ports import ControlCycleStore, ExecutionTransport
from .target_certification import confirmed_boundary
from .timing_certification import preparation_deadline, verify_timing_port
from .validation import require


class PrepareControlCycle:
    def __init__(
        self,
        store: ControlCycleStore,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
        transport: ExecutionTransport | None = None,
    ) -> None:
        self._store, self._clock, self._elapsed = store, clock, elapsed
        self._transport = transport

    def execute(
        self, request: PrepareControlCycleRequest
    ) -> PrepareControlCycleResponse:
        request = PrepareControlCycleRequest.model_validate_json(canonical(request))
        normalized = request.payload.model_copy(
            update={
                "cycle_start_at": request.payload.cycle_start_at.astimezone(
                    timezone.utc
                ),
                "cycle_end_at": request.payload.cycle_end_at.astimezone(timezone.utc),
                "authority": request.payload.authority.model_copy(
                    update={
                        "valid_until": request.payload.authority.valid_until.astimezone(
                            timezone.utc
                        ),
                    }
                ),
            }
        )
        try:
            return self._store.prepare_once(
                request,
                sha256(canonical(request)).hexdigest(),
                sha256(canonical(normalized)).hexdigest(),
                lambda current, target, cursor, pending, recorded: self._prepare(
                    request, current, target, cursor, pending, recorded
                ),
            )
        except InitializationFailure as exc:
            return self._response(request, None, None, exc)

    def _response(
        self,
        request: PrepareControlCycleRequest,
        current: InitializationEvidence | None,
        evidence: ControlCycleEvidence | None,
        failure: InitializationFailure | None = None,
    ) -> PrepareControlCycleResponse:
        now, p = self._clock(), request.payload
        commands: list[DeviceCommand] = []
        predicted = None
        strategy = None
        if evidence:
            assert current is not None
            devices = {d.resource_id: d for d in current.resolved_model_package.devices}
            prepared = evidence.prepared
            states: list[PredictedDeviceState] = []
            for i, resource in enumerate(evidence.domain.devices.resources):
                d = devices[resource.resource_id]
                if d.p_control_enabled or d.q_control_enabled:
                    commands.append(
                        DeviceCommand(
                            command_id=f"{evidence.command_batch_id}:{i}",
                            resource_id=d.resource_id,
                            p_setpoint_mw=prepared.command.p_mw[i]
                            if d.p_control_enabled
                            else None,
                            q_setpoint_mvar=prepared.command.q_mvar[i]
                            if d.q_control_enabled
                            else None,
                        )
                    )
                states.append(
                    PredictedDeviceState(
                        resource_id=d.resource_id,
                        p_mw=prepared.predicted_endpoint.p_mw[i],
                        q_mvar=prepared.predicted_endpoint.q_mvar[i],
                    )
                )
            predicted = PredictedEndpoint(
                device_states=tuple(states),
                storage_energy_mwh=prepared.predicted_endpoint.energy_mwh,
            )
            strategy = (
                "retained_feedback"
                if prepared.feedback_policy
                else "backup"
                if evidence.plan.commands
                else "terminal"
            )
        task = current.request.payload.planning_inputs.energy_task if current else None
        closed = current is not None and current.request.payload.mode == "closed_loop"
        timed = evidence.timed_proof if evidence else None
        timing = (
            current.request.payload.control_policy.bounded_timing if current else None
        )
        return PrepareControlCycleResponse(
            message_id=str(uuid4()),
            correlation_id=request.message_id,
            microgrid_id=request.microgrid_id,
            run_id=request.run_id,
            created_at=now,
            payload=ControlCycleResponsePayload(
                outcome="accepted" if evidence else "rejected",
                errors=(failure.error,) if failure else (),
                data=ControlCycleResult(
                    timed_proof=timed,
                    decision=("dispatchable" if closed else "shadow_only")
                    if evidence
                    else "withheld",
                    cycle_id=p.cycle_id,
                    context_id=p.context_id,
                    snapshot_id=p.snapshot_id,
                    command_batch_id=evidence.command_batch_id if evidence else None,
                    target_id=evidence.target_id if evidence else None,
                    apply_at=timed.apply_not_before_at
                    if timed
                    else p.cycle_start_at
                    if evidence
                    else None,
                    cycle_end_at=p.cycle_end_at if evidence else None,
                    dispatch_deadline_at=(
                        timed.dispatch_deadline_at if timed else p.cycle_start_at
                    )
                    if evidence and closed
                    else None,
                    feedback_due_at=p.cycle_end_at
                    + timedelta(
                        seconds=current.request.payload.control_policy.feedback_timeout_seconds
                    )
                    if evidence and closed and current
                    else None,
                    mode="closed_loop" if closed else "shadow",
                    dispatch_enabled=bool(closed and evidence),
                    time_basis=timing.profile
                    if timing
                    else "software_synchronous_v1"
                    if closed
                    else "synchronous_model_only",
                    authority=p.authority,
                    commands=tuple(commands),
                    predicted_endpoint=predicted,
                    model_check=CycleModelCheck(
                        status="passed" if evidence else "unproven",
                        evidence_ref=f"cycle:{request.run_id}:{evidence.command_batch_id}"
                        if evidence
                        else None,
                        guaranteed_until=p.cycle_end_at if evidence else None,
                    ),
                    strategy_source=strategy,
                    energy_feedback=evidence.prepared.energy_feedback
                    if evidence
                    else False,
                    target_response_deadline_at=evidence.target_response_deadline_at
                    if evidence
                    else None,
                    target_guaranteed_until=evidence.target_guaranteed_until
                    if evidence
                    else None,
                    energy_task_deadline_at=task.deadline_at if task else None,
                    reason=str(failure)
                    if failure
                    else "Bounded asynchronous certificate; dispatch and measured completion remain separate"
                    if timed
                    else "Prepared software command; dispatch and measured completion remain separate"
                    if closed
                    else "Shadow prediction under synchronous model timing; no dispatch or execution confirmation",
                    evaluated_at=now,
                ),
            ),
        )

    def _prepare(
        self,
        request: PrepareControlCycleRequest,
        current: InitializationEvidence | None,
        target: PccTargetEvidence | None,
        cursor: ObservationCursor | None,
        pending: str | None,
        recorded: PrepareControlCycleResponse | None,
    ) -> tuple[PrepareControlCycleResponse, ControlCycleEvidence | None]:
        if recorded is not None:
            # Historical replay never revives a batch head or extends its validity.
            return recorded.model_copy(
                update={
                    "message_id": str(uuid4()),
                    "correlation_id": request.message_id,
                    "created_at": self._clock(),
                }
            ), None
        started = self._elapsed()
        try:
            require(
                current is not None,
                "RUN_NOT_FOUND",
                "Run does not belong to this microgrid",
            )
            assert current is not None
            if current.request.payload.mode == "closed_loop":
                require(
                    self._transport is not None,
                    "TRANSPORT_UNAVAILABLE",
                    "No trusted software execution port is configured",
                )
                assert self._transport is not None
                verify_timing_port(current, self._transport)
                require(
                    self._transport.authorized(
                        request.microgrid_id, current.request.payload.authority
                    ),
                    "AUTHORITY_LOST",
                    "Current authority was not verified by the software transport",
                )
            require(
                pending is None,
                "CYCLE_ALREADY_PREPARED",
                "A shadow batch already exists; reuse its cycle identity",
            )
            evidence = prepare_cycle_evidence(
                request, current, target, cursor, str(uuid4()), self._clock()
            )
            require(
                self._elapsed() - started
                <= current.request.payload.control_policy.compute_budget_seconds,
                "COMPUTE_BUDGET_EXCEEDED",
                "Cycle preparation exceeded its computation budget",
            )
            confirmed_boundary(current, self._clock(), request.created_at)
            if current.request.payload.mode == "closed_loop":
                assert self._transport is not None
                require(
                    request.payload.cycle_start_at
                    <= self._clock()
                    <= preparation_deadline(current),
                    "EXECUTION_TIMING_UNSUPPORTED",
                    "Preparation must complete inside its admitted timing window",
                )
                require(
                    self._transport.authorized(
                        request.microgrid_id, current.request.payload.authority
                    ),
                    "AUTHORITY_LOST",
                    "Authority changed during cycle preparation",
                )
                require(
                    request.payload.cycle_start_at
                    <= self._clock()
                    <= preparation_deadline(current)
                    and self._elapsed() - started
                    <= current.request.payload.control_policy.compute_budget_seconds,
                    "EXECUTION_TIMING_UNSUPPORTED",
                    "Preparation expired during final authority verification",
                )
            return self._response(request, current, evidence), evidence
        except InitializationFailure as exc:
            return self._response(request, current, None, exc), None
        except (ValueError, ArithmeticError) as exc:
            return self._response(
                request,
                current,
                None,
                InitializationFailure("TIMED_CYCLE_UNPROVEN", str(exc)),
            ), None

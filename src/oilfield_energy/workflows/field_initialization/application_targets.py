"""Admit an immutable external target without advancing the confirmed state."""

from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from .contracts import (
    InitializationEvidence,
    InitializationFailure,
    PccGoal,
    PccTargetEvidence,
    PccTargetResponsePayload,
    PccTargetResult,
    RequestPccTargetRequest,
    RequestPccTargetResponse,
)
from .messages import canonical
from .ports import PccTargetStore
from .target_certification import (
    certify_external_target,
    confirmed_boundary,
    previous_strategy_status,
    target_identity,
)
from .validation import require


class RequestPccTarget:
    def __init__(
        self,
        store: PccTargetStore,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        self._store, self._clock, self._elapsed = store, clock, elapsed

    def execute(self, request: RequestPccTargetRequest) -> RequestPccTargetResponse:
        request = RequestPccTargetRequest.model_validate_json(canonical(request))
        try:
            return self._store.request_once(
                request,
                sha256(canonical(request)).hexdigest(),
                target_identity(request.payload),
                lambda current, previous, identity: self._prepare(
                    request, current, previous, identity
                ),
            )
        except InitializationFailure as exc:
            return self._response(request, None, None, failure=exc)

    def _response(
        self,
        request: RequestPccTargetRequest,
        current: InitializationEvidence | None,
        previous: PccTargetEvidence | None,
        *,
        evidence: PccTargetEvidence | None = None,
        failure: InitializationFailure | None = None,
    ) -> RequestPccTargetResponse:
        now = self._clock()
        goal = PccGoal(
            p_import_mw=request.payload.target_p_import_mw,
            q_import_mvar=request.payload.target_q_import_mvar,
        )
        task = current.request.payload.planning_inputs.energy_task if current else None
        return RequestPccTargetResponse(
            message_id=str(uuid4()),
            correlation_id=request.message_id,
            microgrid_id=request.microgrid_id,
            run_id=request.run_id,
            created_at=now,
            payload=PccTargetResponsePayload(
                outcome="accepted" if evidence else "rejected",
                errors=(failure.error,) if failure else (),
                data=PccTargetResult(
                    mode=current.request.payload.mode if current else "shadow",
                    target_id=request.payload.target_id,
                    target_status="accepted" if evidence else "rejected",
                    requested_target=goal,
                    accepted_target=goal if evidence else None,
                    response_deadline_at=request.payload.response_deadline_at,
                    guaranteed_until=evidence.guaranteed_until if evidence else None,
                    context_id=current.domain.context_id if current else None,
                    previous_target_id=previous.request.payload.target_id if previous else None,
                    previous_strategy_status=previous_strategy_status(current, previous, now),
                    reason=str(failure)
                    if failure
                    else "Fixed external target certified; guarantee is limited to guaranteed_until",
                    evidence_ref=f"target:{request.run_id}:{request.message_id}"
                    if evidence
                    else None,
                    energy_task_guarantee="not_assessed"
                    if evidence is None
                    else evidence.timed_proof.energy_task_guarantee
                    if evidence.timed_proof
                    else "not_required"
                    if task is None
                    else "maintained"
                    if evidence.plan.task_continuation is not None
                    or current is not None
                    and current.completed_energy_task
                    else "unproven",
                ),
            ),
        )

    def _prepare(
        self,
        request: RequestPccTargetRequest,
        current: InitializationEvidence | None,
        previous: PccTargetEvidence | None,
        identity: str | None,
    ) -> tuple[RequestPccTargetResponse, PccTargetEvidence | None]:
        started = self._elapsed()
        try:
            require(
                current is not None,
                "RUN_NOT_FOUND",
                "Run does not belong to this microgrid",
            )
            assert current is not None
            require(
                request.payload.context_id == current.domain.context_id,
                "CONTEXT_CHANGED",
                "Expected active context differs",
            )
            require(
                identity is None or identity == target_identity(request.payload),
                "TARGET_ID_CONFLICT",
                "Target ID already denotes different immutable obligations",
            )
            if previous is not None:
                a, b = request.payload, previous.request.payload
                if (a.target_p_import_mw, a.target_q_import_mvar) == (
                    b.target_p_import_mw,
                    b.target_q_import_mvar,
                ):
                    require(
                        a.response_deadline_at == b.response_deadline_at,
                        "TARGET_DEADLINE_CHANGED",
                        "Reissuing the current P/Q target cannot reset its deadline",
                    )
            confirmed_boundary(current, self._clock(), request.created_at)
            try:
                evidence = certify_external_target(current, request)
            except InitializationFailure:
                raise
            except ValueError as exc:
                raise InitializationFailure("TARGET_UNPROVEN", str(exc)) from exc
            require(
                self._elapsed() - started
                <= current.request.payload.control_policy.compute_budget_seconds,
                "COMPUTE_BUDGET_EXCEEDED",
                "Target certification exceeded its admission budget",
            )
            confirmed_boundary(current, self._clock(), request.created_at)
            return self._response(request, current, previous, evidence=evidence), evidence
        except InitializationFailure as exc:
            return self._response(request, current, previous, failure=exc), None

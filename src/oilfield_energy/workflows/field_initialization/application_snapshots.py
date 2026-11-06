"""Observe and assess field facts without committing an execution cycle."""

from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from time import monotonic
from typing import Literal
from uuid import uuid4

from .confirmed import confirmed_snapshot, confirmed_state
from .contracts import (
    InitializationEvidence,
    InitializationFailure,
    InterfaceError,
    ObservationBarrier,
    ObservationCursor,
    PccTargetEvidence,
    SnapshotAssessment,
    SnapshotDecision,
    SnapshotResponsePayload,
    SnapshotResult,
    SubmitSnapshotRequest,
    SubmitSnapshotResponse,
)
from .contracts_values import ArtifactReference
from .messages import canonical
from .ports import SnapshotStore
from .snapshot_assessment import assess_snapshot
from .snapshot_ordering import next_observation_cursor, snapshot_identity
from .validation import require, validate_snapshot_values


class SubmitSnapshot:
    def __init__(
        self,
        store: SnapshotStore,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        self._store, self._clock, self._elapsed = store, clock, elapsed

    def execute(self, request: SubmitSnapshotRequest) -> SubmitSnapshotResponse:
        request = SubmitSnapshotRequest.model_validate_json(canonical(request))
        try:
            return self._store.submit_once(
                request,
                sha256(canonical(request)).hexdigest(),
                snapshot_identity(request.payload),
                lambda current, target, latest, recorded: self._prepare(
                    request, current, target, latest, recorded
                ),
            )
        except InitializationFailure as exc:
            return self._response(
                request, None, (exc.error,), "invalid", "unconfirmed", "rejected", None
            )

    def _response(
        self,
        request: SubmitSnapshotRequest,
        current: InitializationEvidence | None,
        errors: tuple[InterfaceError, ...],
        input_status: Literal["valid", "invalid"],
        state_status: Literal["consistent", "outside_model", "unconfirmed"],
        observation_status: Literal["latest", "archived", "rejected"],
        barrier: ObservationBarrier | None,
        assessment: SnapshotAssessment | None = None,
    ) -> SubmitSnapshotResponse:
        return SubmitSnapshotResponse(
            message_id=str(uuid4()),
            correlation_id=request.message_id,
            microgrid_id=request.microgrid_id,
            run_id=request.run_id,
            created_at=self._clock(),
            payload=SnapshotResponsePayload(
                outcome="rejected" if errors else "accepted",
                errors=errors,
                data=SnapshotResult(
                    observed_box=assessment.observed_box if assessment else None,
                    state_semantics=assessment.state_semantics
                    if assessment
                    else "bounded_set"
                    if current
                    and current.request.payload.control_policy.observation_uncertainty
                    else "exact",
                    mode=current.request.payload.mode if current else "shadow",
                    snapshot_id=request.payload.snapshot_id,
                    context_id=current.domain.context_id if current else None,
                    evaluated_at=self._clock(),
                    input_status=input_status,
                    state_status=state_status,
                    observation_status=observation_status,
                    guarantee_status=barrier.guarantee_status
                    if barrier
                    else "unconfirmed"
                    if current
                    else "not_assessed",
                    resync_required=barrier is not None,
                    errors=errors,
                    evidence_ref=f"snapshot:{request.run_id}:{request.message_id}"
                    if observation_status != "rejected"
                    else None,
                ),
            ),
        )

    def _prepare(
        self,
        request: SubmitSnapshotRequest,
        current: InitializationEvidence | None,
        target: PccTargetEvidence | None,
        latest: ObservationCursor | None,
        recorded: SubmitSnapshotResponse | None,
    ) -> SnapshotDecision:
        if recorded is not None:
            # Same immutable snapshot under a new envelope: historical assessment,
            # not a new quality check or a chance to clear an existing barrier.
            return SnapshotDecision(
                response=recorded.model_copy(
                    update={
                        "message_id": str(uuid4()),
                        "correlation_id": request.message_id,
                        "created_at": self._clock(),
                    }
                )
            )
        started = self._elapsed()
        eligible = False
        cursor: ObservationCursor | None = None
        input_status: Literal["valid", "invalid"] = "invalid"
        state_status: Literal["consistent", "outside_model", "unconfirmed"] = (
            "unconfirmed"
        )
        assessment: SnapshotAssessment | None = None
        barrier = current.observation_barrier if current else None
        errors: tuple[InterfaceError, ...] = ()
        try:
            require(
                current is not None,
                "RUN_NOT_FOUND",
                "Run does not belong to this microgrid",
            )
            assert current is not None
            s, base, now = request.payload, current.request.payload, self._clock()
            require(
                s.context_id == current.domain.context_id,
                "CONTEXT_CHANGED",
                "Expected active context differs",
            )
            require(
                s.snapshot_at.second == 0 and s.snapshot_at.microsecond == 0,
                "INVALID_MESSAGE",
                "snapshot_at must be a minute boundary",
            )
            require(
                s.snapshot_at <= s.received_at <= now
                and 0
                <= (now - request.created_at).total_seconds()
                <= base.control_policy.telemetry_max_age_seconds,
                "TELEMETRY_INVALID",
                "Snapshot/receipt/message time is invalid or future",
            )
            cursor = next_observation_cursor(s, base.initial_snapshot, latest)
            eligible = True
            mapping = current.resolved_model_package.point_mapping
            assert not isinstance(mapping, ArtifactReference)
            validate_snapshot_values(
                s,
                current.resolved_model_package,
                base.control_policy,
                base.authority,
                current.domain.network.buses,
                current.domain.network.buses[current.domain.network.slack],
                mapping,
                now,
                request.created_at,
            )
            require(
                s.operating_state_ref == base.initial_snapshot.operating_state_ref,
                "CONTEXT_CHANGED",
                "Operating state changed; model resynchronization is required",
            )
            input_status = "valid"
            assessment, errors = assess_snapshot(current, s, target)
            state_status = "outside_model" if errors else "consistent"
            if errors and all(
                e.code == "FORECAST_COVERAGE_INSUFFICIENT" for e in errors
            ):
                state_status = "unconfirmed"
            require(
                self._elapsed() - started <= base.control_policy.compute_budget_seconds,
                "COMPUTE_BUDGET_EXCEEDED",
                "Snapshot assessment exceeded its admission budget",
            )
            # A result that became stale during numerical work cannot release a guard.
            validate_snapshot_values(
                s,
                current.resolved_model_package,
                base.control_policy,
                base.authority,
                current.domain.network.buses,
                current.domain.network.buses[current.domain.network.slack],
                mapping,
                self._clock(),
                request.created_at,
            )
        except InitializationFailure as exc:
            errors = (*errors, exc.error)
            state_status = (
                "outside_model"
                if exc.error.code
                in ("CONTEXT_CHANGED", "AUTHORITY_LOST", "MODEL_UNSUPPORTED")
                and eligible
                else "unconfirmed"
            )
            input_status = "invalid"
        if eligible and current is not None:
            observed_changed = (
                assessment is None
                or assessment.observed_state != confirmed_state(current)
                or request.payload.snapshot_at
                != confirmed_snapshot(current).snapshot_at
            )
            pending = current.pending_cycle
            expected_feedback = (
                pending is not None
                and current.pending_sent
                and current.field_run_state in ("awaiting_feedback", "stopped")
                and request.payload.snapshot_at == pending.request.payload.cycle_end_at
                and assessment is not None
            )
            if expected_feedback and not errors:
                # Receiving the expected endpoint is still not cycle confirmation.
                observed_changed = False
            if errors or observed_changed:
                status: Literal["invalidated", "unconfirmed"] = (
                    "invalidated"
                    if state_status == "outside_model"
                    or any(
                        e.code
                        in (
                            "OBSERVATION_OUTSIDE_ENVELOPE",
                            "STATE_OUTSIDE_CERTIFICATE",
                            "SNAPSHOT_SAFETY_UNPROVEN",
                        )
                        for e in errors
                    )
                    else "unconfirmed"
                )
                new_barrier = ObservationBarrier(
                    snapshot_id=request.payload.snapshot_id,
                    guarantee_status=status,
                    error=errors[0]
                    if errors
                    else InterfaceError(
                        code="CONFIRMED_STATE_REQUIRED",
                        message="A new observation is not a confirmed execution state; explicit resynchronization is required",
                    ),
                )
                if barrier is None or (
                    barrier.guarantee_status != "invalidated"
                    and status == "invalidated"
                ):
                    barrier = new_barrier
        return SnapshotDecision(
            response=self._response(
                request,
                current,
                errors,
                input_status,
                state_status,
                "latest" if eligible else "archived",
                barrier,
                assessment,
            ),
            advance_observation=eligible,
            barrier=barrier if eligible else None,
            assessment=assessment,
            cursor=cursor if eligible else None,
        )

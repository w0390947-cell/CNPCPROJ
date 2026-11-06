"""Admit revised inputs without inventing a newer confirmed device state."""

from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Literal
from uuid import uuid4

from .certification import certify_conditions
from .confirmed import confirmed_snapshot
from .contracts import (
    InitializationEvidence,
    InitializationFailure,
    InitializeRequest,
    PccTargetEvidence,
    PlanningInputs,
    PlanningUpdatePayload,
    PlanningUpdateResult,
    UpdatePlanningRequest,
    UpdatePlanningResponse,
)
from .contracts_values import ArtifactReference, Snapshot
from .domain import map_planning_domain
from .messages import canonical
from .ports import PlanningUpdateStore
from .target_certification import certify_external_target
from .validation import require, validate_snapshot


class UpdatePlanningInputs:
    def __init__(
        self,
        store: PlanningUpdateStore,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        self._store, self._clock, self._elapsed = store, clock, elapsed

    def execute(self, request: UpdatePlanningRequest) -> UpdatePlanningResponse:
        request = UpdatePlanningRequest.model_validate_json(canonical(request))
        fingerprint = sha256(canonical(request)).hexdigest()
        try:
            return self._store.update_once(
                request,
                fingerprint,
                lambda current, revisions, previous: self._prepare(
                    request, current, revisions, previous
                ),
            )
        except InitializationFailure as exc:
            return self._response(request, None, "rejected", str(exc), failure=exc)

    def _response(
        self,
        request: UpdatePlanningRequest,
        current: InitializationEvidence | None,
        status: Literal["staged", "activated", "rejected"],
        reason: str,
        *,
        evidence: InitializationEvidence | None = None,
        failure: InitializationFailure | None = None,
        previous: PccTargetEvidence | None = None,
        target_evidence: PccTargetEvidence | None = None,
    ) -> UpdatePlanningResponse:
        task = current.request.payload.planning_inputs.energy_task if current else None
        joint_plan = (
            target_evidence.plan
            if target_evidence
            else evidence.plan
            if evidence
            else None
        )
        joint_timed = (
            target_evidence.timed_proof
            if target_evidence
            else evidence.timed_proof
            if evidence
            else None
        )
        return UpdatePlanningResponse(
            message_id=str(uuid4()),
            correlation_id=request.message_id,
            microgrid_id=request.microgrid_id,
            run_id=request.run_id,
            created_at=self._clock(),
            payload=PlanningUpdatePayload(
                outcome="rejected" if failure else "accepted",
                errors=(failure.error,) if failure else (),
                data=PlanningUpdateResult(
                    mode=current.request.payload.mode if current else "shadow",
                    input_revision=request.payload.input_revision,
                    activation_status=status,
                    new_context_id=evidence.domain.context_id if evidence else None,
                    effective_from=request.payload.effective_from,
                    valid_until=min(
                        request.payload.valid_until,
                        current.request.payload.authority.valid_until,
                        current.request.payload.control_policy.observation_uncertainty.valid_until
                        if current.request.payload.control_policy.observation_uncertainty
                        else request.payload.valid_until,
                    )
                    if current
                    else request.payload.valid_until,
                    preserved_task_id=task.task_id if task else None,
                    preserved_target_deadline_at=previous.request.payload.response_deadline_at
                    if previous
                    else None,
                    reason=reason,
                    evidence_ref=f"planning:{request.run_id}:{request.message_id}"
                    if evidence
                    else None,
                    energy_task_guarantee="not_assessed"
                    if evidence is None
                    else joint_timed.energy_task_guarantee
                    if joint_timed
                    else "not_required"
                    if task is None
                    else "maintained"
                    if evidence.completed_energy_task
                    or joint_plan is not None
                    and joint_plan.task_continuation is not None
                    else "unproven",
                ),
            ),
        )

    def _prepare(
        self,
        request: UpdatePlanningRequest,
        current: InitializationEvidence | None,
        revisions: frozenset[str],
        previous: PccTargetEvidence | None = None,
    ) -> tuple[
        UpdatePlanningResponse, InitializationEvidence | None, PccTargetEvidence | None
    ]:
        started = self._elapsed()
        try:
            require(
                current is not None,
                "RUN_NOT_FOUND",
                "Run does not belong to this microgrid",
            )
            assert current is not None
            now, update = self._clock(), request.payload
            base = current.request.payload
            require(
                update.context_id == current.domain.context_id,
                "CONTEXT_CHANGED",
                "Expected active context differs",
            )
            require(
                update.input_revision not in revisions,
                "INPUT_REVISION_CONFLICT",
                "Input revision is already reserved; replay its original message",
            )
            require(
                0
                <= (now - request.created_at).total_seconds()
                <= base.control_policy.telemetry_max_age_seconds,
                "INVALID_MESSAGE_TIME",
                "Request is stale or future",
            )
            require(
                base.authority.valid_until > now
                and base.authority.valid_until > update.effective_from,
                "AUTHORITY_LOST",
                "Declared authority does not cover the proposed boundary",
            )
            require(
                update.issued_at <= now < update.valid_until,
                "INPUT_EXPIRED_OR_FUTURE",
                "Planning inputs have expired or were issued in the future",
            )
            old = base.planning_inputs
            require(
                update.issued_at >= old.issued_at
                and update.effective_from >= old.effective_from,
                "INPUT_OUT_OF_ORDER",
                "Update predates active planning inputs",
            )
            require(
                update.energy_task == old.energy_task,
                "TASK_CHANGE_NOT_SUPPORTED",
                "Preserve the original task, bounds, deadline, required flag and source; task changes need separate admission",
            )
            plan = PlanningInputs.model_validate(
                {**update.model_dump(), "context_id": None}
            )
            # Structural/time mapping is also required for staged data; no future state is synthesized.
            map_planning_domain(
                current.resolved_model_package,
                plan.model_copy(update={"energy_task": None})
                if current.completed_energy_task
                else plan,
                base.control_policy,
                current.domain.network,
                "pending:" + sha256(canonical(request)).hexdigest(),
            )
            candidate = InitializeRequest.model_validate(
                {
                    **current.request.model_dump(),
                    "message_id": request.message_id,
                    "created_at": request.created_at,
                    "payload": {
                        **base.model_dump(),
                        "initial_snapshot": Snapshot.model_validate(
                            confirmed_snapshot(current).model_dump()
                            | {"context_id": None}
                        ).model_dump(),
                        "planning_inputs": plan.model_dump(),
                        "model_package": current.resolved_model_package.model_dump(),
                    },
                }
            )
            now = self._clock()
            require(
                self._elapsed() - started <= base.control_policy.compute_budget_seconds,
                "COMPUTE_BUDGET_EXCEEDED",
                "Planning validation exceeded its admission budget",
            )
            require(
                now < min(plan.valid_until, base.authority.valid_until),
                "INPUT_EXPIRED_OR_FUTURE",
                "Inputs or authority expired during validation",
            )
            reason = self._waiting_reason(candidate, current, now)
            if reason:
                return (
                    self._response(
                        request, current, "staged", reason, previous=previous
                    ),
                    None,
                    None,
                )
            mapping = current.resolved_model_package.point_mapping
            assert not isinstance(mapping, ArtifactReference)
            evidence = certify_conditions(
                candidate,
                current.domain.network,
                mapping,
                self._clock,
                self._elapsed,
                started,
                completed_energy_task=current.completed_energy_task,
            )
            target_evidence = None
            if previous is not None:
                try:
                    target_evidence = certify_external_target(
                        evidence,
                        previous.request,
                        minimum_guaranteed_until=previous.guaranteed_until,
                    )
                except ValueError as exc:
                    raise InitializationFailure(
                        "RETAINED_TARGET_UNPROVEN", str(exc)
                    ) from exc
            require(
                self._elapsed() - started <= base.control_policy.compute_budget_seconds,
                "COMPUTE_BUDGET_EXCEEDED",
                "Planning and target revalidation exceeded the admission budget",
            )
            # Computation may cross the control boundary; a historical state cannot be promoted.
            reason = self._waiting_reason(candidate, current, self._clock())
            if reason:
                return (
                    self._response(
                        request, current, "staged", reason, previous=previous
                    ),
                    None,
                    None,
                )
            return (
                self._response(
                    request,
                    current,
                    "activated",
                    "Revalidated at the confirmed shadow boundary",
                    evidence=evidence,
                    previous=previous,
                    target_evidence=target_evidence,
                ),
                evidence,
                target_evidence,
            )
        except InitializationFailure as exc:
            return (
                self._response(
                    request,
                    current,
                    "rejected",
                    str(exc),
                    failure=exc,
                    previous=previous,
                ),
                None,
                None,
            )
        except ValueError as exc:
            failure = InitializationFailure("INVALID_PLANNING_UPDATE", str(exc))
            return (
                self._response(
                    request,
                    current,
                    "rejected",
                    str(exc),
                    failure=failure,
                    previous=previous,
                ),
                None,
                None,
            )

    def _waiting_reason(
        self,
        candidate: InitializeRequest,
        current: InitializationEvidence,
        now: datetime,
    ) -> str:
        if current.observation_barrier is not None:
            return "RESYNC_REQUIRED: new field observations prevent activation against the old confirmed state"
        if current.field_run_state in (
            "awaiting_feedback",
            "resync_required",
            "stopped",
        ):
            return "EXECUTION_NOT_READY: pending execution or blocked/stopped run prevents activation"
        p = candidate.payload
        if p.planning_inputs.effective_from > now:
            return (
                "FUTURE_BOUNDARY: await a confirmed snapshot at the requested boundary"
            )
        at = p.initial_snapshot.snapshot_at
        timing = p.control_policy.bounded_timing
        if timing and now > at + timedelta(seconds=timing.prepare_max_seconds):
            return "CONFIRMED_STATE_REQUIRED: bounded preparation window has elapsed"
        if p.planning_inputs.effective_from != at or now >= at + timedelta(minutes=1):
            return "CONFIRMED_STATE_REQUIRED: no confirmed snapshot at this control boundary"
        mapping = current.resolved_model_package.point_mapping
        assert not isinstance(mapping, ArtifactReference)
        network = current.domain.network
        try:
            validate_snapshot(
                candidate, network.buses, network.buses[network.slack], mapping, now
            )
        except InitializationFailure as exc:
            if exc.error.code != "TELEMETRY_INVALID":
                raise
            return "STALE_CONFIRMED_STATE: " + str(exc)
        return ""

"""Field prerequisites and pure one-minute prediction using retained proofs."""

from datetime import datetime, timedelta

from oilfield_energy.modules.control.contracts import TubeTransition
from oilfield_energy.workflows.single_execution.api import (
    prepare_certified_cycle,
    unified_network_box,
)
from oilfield_energy.workflows.single_execution.contracts import PreparedUnifiedCycle

from .confirmed import (
    confirmed_box,
    confirmed_snapshot,
    confirmed_state,
    confirmed_step,
)
from .contracts import (
    ArtifactReference,
    InitializationEvidence,
    InitializationFailure,
    ObservationCursor,
    PccTargetEvidence,
    SubmittedSnapshot,
)
from .contracts_cycles import ControlCycleEvidence, PrepareControlCycleRequest
from .observation_uncertainty import contains_box
from .snapshot_assessment import assess_snapshot
from .target_certification import confirmed_boundary
from .timing_certification import certify_timed_continuation, midpoint
from .validation import require, validate_snapshot_values


def prepare_cycle_evidence(
    request: PrepareControlCycleRequest,
    current: InitializationEvidence,
    target: PccTargetEvidence | None,
    cursor: ObservationCursor | None,
    batch_id: str,
    now: datetime,
) -> ControlCycleEvidence:
    p, base = request.payload, current.request.payload
    actual = confirmed_state(current)
    boundary = confirmed_snapshot(current)
    step = confirmed_step(current)
    require(
        p.context_id == current.domain.context_id,
        "CONTEXT_CHANGED",
        "Expected active context differs",
    )
    require(
        p.authority == base.authority,
        "AUTHORITY_CHANGED",
        "Authority must match the active admitted lease",
    )
    require(
        any(
            d.p_control_enabled or d.q_control_enabled
            for d in current.resolved_model_package.devices
        ),
        "NO_AUTHORIZED_COMMANDS",
        "No device axis is authorized for a shadow command batch",
    )
    confirmed_boundary(current, now, request.created_at)
    require(
        p.cycle_start_at == boundary.snapshot_at,
        "CONFIRMED_STATE_REQUIRED",
        "Preparation must start at the current measured confirmed boundary",
    )
    require(
        p.cycle_end_at == p.cycle_start_at + timedelta(seconds=60),
        "CYCLE_TIME_INVALID",
        "A cycle must span exactly 60 seconds",
    )
    require(
        p.cycle_end_at
        <= min(
            current.valid_until,
            base.authority.valid_until,
            base.planning_inputs.valid_until,
        )
        and step + 1 < len(current.domain.forecasts),
        "FORECAST_COVERAGE_INSUFFICIENT",
        "The entire minute requires explicit input and authority coverage",
    )
    snapshot = (
        cursor.snapshot
        if cursor and cursor.snapshot.context_id == p.context_id
        else SubmittedSnapshot.model_validate(
            boundary.model_dump() | {"context_id": p.context_id}
        )
    )
    require(
        p.snapshot_id == snapshot.snapshot_id,
        "SNAPSHOT_NOT_CURRENT",
        "Preparation must name the latest observation for this context",
    )
    mapping = current.resolved_model_package.point_mapping
    assert not isinstance(mapping, ArtifactReference)
    validate_snapshot_values(
        snapshot,
        current.resolved_model_package,
        base.control_policy,
        base.authority,
        current.domain.network.buses,
        current.domain.network.buses[current.domain.network.slack],
        mapping,
        now,
        request.created_at,
    )
    assessment, errors = assess_snapshot(current, snapshot, target)
    if errors:
        raise InitializationFailure(errors[0].code, errors[0].message)
    require(
        snapshot.snapshot_at == p.cycle_start_at
        and (
            base.control_policy.observation_uncertainty is not None
            and assessment.observed_box is not None
            and contains_box(assessment.observed_box, confirmed_box(current))
            or assessment.observed_state == actual
        ),
        "RESYNC_REQUIRED",
        "Observation must match the current confirmed device state and boundary",
    )
    observed = assessment.observed_inputs
    assert observed is not None
    if base.control_policy.bounded_timing:
        require(
            target is None
            or (
                target.context_id == p.context_id
                and target.guaranteed_until >= p.cycle_end_at
            ),
            "TARGET_UNAVAILABLE",
            "A timed cycle cannot silently drop an expired or different target",
        )
        retained = (
            current.confirmed_boundary.timed_continuation
            if current.confirmed_boundary
            else current.timed_proof
        )
        try:
            timed = certify_timed_continuation(current, target)
        except (ValueError, ArithmeticError):
            if retained is None:
                raise
            timed = certify_timed_continuation(current, target, retained=retained)
        domain = target.domain if target else current.domain
        command = timed.continuation_commands[0]
        prepared = PreparedUnifiedCycle(
            command=command,
            predicted_endpoint=midpoint(timed.endpoint),
            transition=TubeTransition(
                initial=confirmed_box(current),
                command=command,
                endpoint=timed.endpoint,
                path=timed.path,
            ),
            path_network=unified_network_box(
                domain, timed.path, domain.forecasts[step]
            ),
            endpoint_network=unified_network_box(
                domain, timed.endpoint, domain.forecasts[step + 1]
            ),
            expected_next=timed.endpoint,
            feedback_policy=False,
            energy_feedback=False,
        )
        return ControlCycleEvidence(
            request=request,
            command_batch_id=batch_id,
            snapshot=snapshot,
            domain=domain,
            plan=target.plan if target else current.plan,
            observed=observed,
            prepared=prepared,
            target_id=target.request.payload.target_id if target else None,
            target_response_deadline_at=target.request.payload.response_deadline_at
            if target
            else None,
            target_guaranteed_until=target.guaranteed_until if target else None,
            timed_proof=timed,
        )
    domain, plan = current.domain, current.plan
    if target is not None:
        require(
            target.context_id == p.context_id
            and target.guaranteed_until >= p.cycle_end_at,
            "TARGET_UNAVAILABLE",
            "Accepted target must remain certified for the whole cycle; no fallback can drop it",
        )
        domain, plan = target.domain, target.plan
        tracking = plan.tracking
        obligation = target.request.payload
        require(
            tracking is not None,
            "TARGET_UNAVAILABLE",
            "Accepted target has no tracking policy",
        )
        assert tracking is not None
        origin = base.planning_inputs.effective_from
        require(
            (tracking.target_p_mw, tracking.target_q_mvar)
            == (obligation.target_p_import_mw, obligation.target_q_import_mvar)
            and origin + timedelta(minutes=tracking.deadline_step)
            == obligation.response_deadline_at
            and origin + timedelta(minutes=tracking.guaranteed_until_step)
            == target.guaranteed_until
            and domain.p_band_mw == obligation.p_tolerance_mw
            and domain.q_band_mvar == obligation.q_tolerance_mvar
            and len(tracking.policies) > step - plan.start_step,
            "TARGET_UNAVAILABLE",
            "Retained certificate must preserve the accepted target, bands and original deadline",
        )
    expected = (
        current.confirmed_boundary.expected
        if current.confirmed_boundary
        else plan.initial
    )
    if plan.start_step == step:
        expected = plan.initial
    elif plan.tracking is not None and 0 <= step - plan.start_step < len(
        plan.tracking.tubes
    ):
        expected = plan.tracking.tubes[step - plan.start_step]
    require(
        plan.start_step + len(plan.commands) < len(domain.forecasts),
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Continuation exceeds explicit forecast coverage",
    )
    task = base.planning_inputs.energy_task
    require(
        not task
        or not task.required
        or current.completed_energy_task
        or plan.task_continuation is not None,
        "ENERGY_TASK_UNPROVEN",
        "Required original energy task has no retained continuation",
    )
    try:
        prepared = prepare_certified_cycle(
            domain, actual, expected, step, plan, observed
        )
    except (ValueError, ArithmeticError) as exc:
        raise InitializationFailure("CYCLE_UNPROVEN", str(exc)) from exc
    return ControlCycleEvidence(
        request=request,
        command_batch_id=batch_id,
        snapshot=snapshot,
        domain=domain,
        plan=plan,
        observed=observed,
        prepared=prepared,
        target_id=target.request.payload.target_id if target else None,
        target_response_deadline_at=target.request.payload.response_deadline_at
        if target
        else None,
        target_guaranteed_until=target.guaranteed_until if target else None,
    )

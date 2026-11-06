"""Execution identity, monotone acknowledgements and observed completion checks."""

from datetime import datetime

from oilfield_energy.modules.control.api import tube_contains
from oilfield_energy.modules.control.contracts import TubeBox, TubeState

from .contracts import InitializationFailure
from .contracts_execution import CommandResult, ExecutionBatch, ExecutionReport
from .contracts_snapshots import SubmittedSnapshot
from .execution_context import ExecutionContext
from .observation_uncertainty import intersect_boxes, window
from .snapshot_assessment import assess_snapshot
from .timing_certification import midpoint
from .validation import require, validate_snapshot_values


def validate_report_energy(
    report: ExecutionReport,
    batch: ExecutionBatch,
    snapshot: SubmittedSnapshot,
    *,
    uncertain: bool,
) -> None:
    energy = report.energy_evidence
    require(
        energy is not None,
        "ENERGY_EVIDENCE_REQUIRED",
        "Storage sample evidence is required",
    )
    assert energy is not None
    sample = next(
        (d.energy_mwh for d in snapshot.device_states if d.energy_mwh is not None), None
    )
    require(
        sample is not None,
        "ENERGY_EVIDENCE_REQUIRED",
        "Referenced snapshot has no storage energy sample",
    )
    assert sample is not None
    require(
        energy.method
        == (
            "bounded_estimate"
            if sample.source_kind == "estimated"
            else "measured_energy"
        )
        and (uncertain or energy.method == "measured_energy"),
        "ENERGY_EVIDENCE_INVALID",
        "Energy evidence must preserve its measured or admitted estimated provenance",
    )
    require(
        energy.interval_start == batch.evidence.request.payload.cycle_start_at
        and energy.interval_end
        == snapshot.snapshot_at
        == batch.evidence.request.payload.cycle_end_at
        and energy.source_ref == sample.source_ref
        and energy.energy_end_mwh == sample.value,
        "ENERGY_EVIDENCE_INVALID",
        "Energy evidence must refer to the unchanged original cycle-end sample",
    )
    if uncertain:
        require(
            sample.uncertainty is not None
            and energy.sample_observed_at == sample.observed_at
            and energy.uncertainty_mwh == sample.uncertainty.absolute_error,
            "ENERGY_EVIDENCE_INVALID",
            "Calibrated error and original sample time must match the archived observation",
        )
    else:
        require(
            energy.uncertainty_mwh == 0
            and (
                energy.sample_observed_at is None
                or energy.sample_observed_at == sample.observed_at
            ),
            "ENERGY_EVIDENCE_INVALID",
            "An exact energy report cannot claim a different sample or nonzero uncertainty",
        )


def merge_results(
    batch: ExecutionBatch, incoming: tuple[CommandResult, ...], now: datetime
) -> tuple[CommandResult, ...]:
    expected = {x.command_id: x.resource_id for x in batch.commands}
    commands = {x.command_id: x for x in batch.commands}
    known = {x.command_id: x for x in batch.results}
    for result in incoming:
        timed = batch.evidence.timed_proof
        uncertain = timed is not None and timed.profile == "bounded_uncertain_v1"
        clock = result.timestamp_uncertainty
        if uncertain and (
            result.acknowledged_at is not None or result.applied_at is not None
        ):
            require(
                clock is not None,
                "CLOCK_EVIDENCE_REQUIRED",
                "Execution timestamps require clock evidence",
            )
            assert clock is not None and timed is not None
            require(
                clock.clock_revision == timed.clock_revision
                and clock.error_seconds == timed.clock_error_bound_seconds,
                "CLOCK_EVIDENCE_INVALID",
                "Execution clock differs from its immutable batch profile",
            )
        else:
            require(
                clock is None,
                "UNCERTAINTY_NOT_ADMITTED",
                "This event does not admit clock uncertainty",
            )
        require(
            expected.get(result.command_id) == result.resource_id,
            "IDENTITY_MISMATCH",
            "Execution result does not name an original command",
        )
        command = commands[result.command_id]
        for echo, requested in (
            (result.echoed_p_setpoint_mw, command.p_setpoint_mw),
            (result.echoed_q_setpoint_mvar, command.q_setpoint_mvar),
        ):
            require(
                echo is None or echo == requested,
                "COMMAND_ECHO_MISMATCH",
                "Setpoint echo differs from an authorized write",
            )
        for at in (result.acknowledged_at, result.applied_at):
            lo, hi = window(at, clock) if at is not None else (now, now)
            if at is not None and clock is not None:
                require(
                    clock.valid_from <= lo <= hi < clock.valid_until,
                    "CLOCK_EVIDENCE_INVALID",
                    "Clock evidence does not cover the event-time range",
                )
            require(
                at is None
                or (
                    batch.send_claimed_at
                    or batch.evidence.request.payload.cycle_start_at
                )
                <= lo
                <= hi
                <= now,
                "INVALID_EXECUTION_TIME",
                "Acknowledgement time is outside the actual execution interval",
            )
        if result.device_status == "applied":
            timed = batch.evidence.timed_proof
            require(
                (
                    timed is None
                    and result.applied_at
                    == batch.evidence.request.payload.cycle_start_at
                )
                or (
                    timed is not None
                    and result.applied_at is not None
                    and (timed.physical_apply_earliest_at or timed.apply_not_before_at)
                    <= window(result.applied_at, clock)[0]
                    <= window(result.applied_at, clock)[1]
                    <= (timed.physical_apply_latest_at or timed.apply_not_after_at)
                    and batch.send_claimed_at is not None
                    and result.applied_at >= batch.send_claimed_at
                ),
                "EXECUTION_TIMING_UNSUPPORTED",
                "Application time lies outside the batch's admitted timing window",
            )
        prior = known.get(result.command_id)
        if prior:
            updates = result.model_dump()
            for field in ("transport_status", "device_status"):
                old, new = getattr(prior, field), getattr(result, field)
                require(
                    old == "unknown" or new == "unknown" or old == new,
                    "EXECUTION_RESULT_CONFLICT",
                    "A known command outcome cannot be reversed",
                )
                updates[field] = old if new == "unknown" else new
            for field in (
                "acknowledged_at",
                "applied_at",
                "echoed_p_setpoint_mw",
                "echoed_q_setpoint_mvar",
            ):
                old, new = getattr(prior, field), getattr(result, field)
                require(
                    old is None or new is None or old == new,
                    "EXECUTION_RESULT_CONFLICT",
                    "Command acknowledgement evidence changed",
                )
                updates[field] = old if new is None else new
            require(
                prior.timestamp_uncertainty is None
                or clock is None
                or prior.timestamp_uncertainty == clock,
                "EXECUTION_RESULT_CONFLICT",
                "Known event clock evidence is immutable",
            )
            updates["timestamp_uncertainty"] = prior.timestamp_uncertainty or clock
            result = CommandResult.model_validate(updates)
        known[result.command_id] = result
    timing = batch.evidence.timed_proof
    if timing:
        times = [
            window(r.applied_at, r.timestamp_uncertainty)
            for r in known.values()
            if r.device_status == "applied" and r.applied_at is not None
        ]
        # The rectangular proof covers arbitrary ordering; enforce the additional advertised skew contract.
        require(
            not times
            or (max(t[1] for t in times) - min(t[0] for t in times)).total_seconds()
            <= timing.application_skew_max_seconds,
            "EXECUTION_TIMING_UNSUPPORTED",
            "Device application skew exceeded the admitted bound",
        )
    return tuple(known[k] for k in sorted(known))


def check_completion(
    ctx: ExecutionContext, report: ExecutionReport, batch: ExecutionBatch, now: datetime
) -> tuple[TubeState, TubeBox | None]:
    current, snapshot = ctx.current, ctx.snapshot
    assert current is not None
    p = current.request.payload
    e = batch.evidence
    require(
        snapshot is not None
        and ctx.cursor is not None
        and ctx.cursor.snapshot.snapshot_id == snapshot.snapshot_id,
        "SNAPSHOT_NOT_CURRENT",
        "Completion requires the latest full observed snapshot",
    )
    assert snapshot is not None
    require(
        snapshot.context_id == e.domain.context_id == current.domain.context_id
        and snapshot.snapshot_at == e.request.payload.cycle_end_at,
        "CONTEXT_CHANGED",
        "Snapshot must match the batch context and exact cycle end",
    )
    require(
        now <= batch.feedback_due_at and report.reported_at <= now,
        "FEEDBACK_TIMEOUT",
        "Cycle feedback missed its confirmation deadline",
    )
    require(
        report.reported_at >= snapshot.received_at,
        "INVALID_EXECUTION_TIME",
        "Cycle-end report predates receipt of its measured snapshot",
    )
    mapping = current.resolved_model_package.point_mapping
    assert isinstance(mapping, tuple)
    validate_snapshot_values(
        snapshot,
        current.resolved_model_package,
        p.control_policy,
        p.authority,
        current.domain.network.buses,
        current.domain.network.buses[current.domain.network.slack],
        mapping,
        now,
        now,
    )
    require(
        snapshot.operating_state_ref == p.initial_snapshot.operating_state_ref,
        "CONTEXT_CHANGED",
        "Operating state changed during execution",
    )
    assessed, errors = assess_snapshot(current, snapshot, ctx.target)
    if errors:
        raise InitializationFailure(errors[0].code, errors[0].message)
    state = assessed.observed_state
    assert state is not None
    require(
        p.control_policy.observation_uncertainty is not None
        or tube_contains(e.prepared.expected_next, state),
        "STATE_OUTSIDE_CERTIFICATE",
        "Measured endpoint left the retained continuation tube",
    )
    results = {r.command_id: r for r in batch.results}
    for d in current.resolved_model_package.devices:
        # Device order in external packages is not the internal resource axis.
        axis = next(
            j
            for j, r in enumerate(e.domain.devices.resources)
            if r.resource_id == d.resource_id
        )
        if not (d.p_control_enabled or d.q_control_enabled):
            continue
        r = results.get(f"{e.command_batch_id}:{axis}")
        require(
            r is not None
            and r.transport_status == "accepted"
            and r.device_status == "applied"
            and r.acknowledged_at is not None
            and r.applied_at is not None,
            "PARTIAL_OR_UNKNOWN_EXECUTION",
            "Every authorized command needs delivery and application evidence",
        )
        assert r is not None
        for enabled, echo, value in (
            (
                d.p_control_enabled,
                r.echoed_p_setpoint_mw,
                e.prepared.command.p_mw[axis],
            ),
            (
                d.q_control_enabled,
                r.echoed_q_setpoint_mvar,
                e.prepared.command.q_mvar[axis],
            ),
        ):
            require(
                not enabled or echo == value,
                "COMMAND_ECHO_MISMATCH",
                "Applied setpoint echo differs from the certified command",
            )
    validate_report_energy(
        report,
        batch,
        snapshot,
        uncertain=p.control_policy.observation_uncertainty is not None,
    )
    if p.control_policy.observation_uncertainty:
        held = {h.resource_id: h for h in snapshot.held_setpoints or ()}
        require(
            all(
                held[r.resource_id].p_mw == e.prepared.command.p_mw[i]
                and held[r.resource_id].q_mvar == e.prepared.command.q_mvar[i]
                for i, r in enumerate(e.domain.devices.resources)
            ),
            "COMMAND_HELD_MISMATCH",
            "Observed held setpoints contradict the original applied batch",
        )
        assert assessed.observed_box is not None and e.timed_proof is not None
        # Full application evidence above establishes this physical reachable set.
        # No network limit, target band or energy-task bound is used to contract it.
        result = intersect_boxes(assessed.observed_box, e.timed_proof.endpoint)
        return midpoint(result), result
    return state, None

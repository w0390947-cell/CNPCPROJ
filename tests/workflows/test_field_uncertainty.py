"""Calibrated intervals and event-time ranges through the actual field workflow."""

import sqlite3
from copy import deepcopy
from datetime import timedelta

import pytest

from tests.support.field_initialization import NOW
from tests.support.field_uncertainty import (
    UncertainHarness,
    UncertainTransport,
    clock_evidence,
    scalar_values,
    uncertainty_document,
)


def initial_result(root, document):
    from oilfield_energy.bootstrap.field_initialization import create_field_initializer
    from oilfield_energy.workflows.field_initialization.contracts import (
        InitializeRequest,
    )
    from tests.support.field_timing import closed_loop_document

    transport = UncertainTransport(
        lambda: NOW + timedelta(seconds=1),
        lambda _: None,
        document["payload"]["control_policy"]["observation_uncertainty"],
        document["payload"]["control_policy"]["bounded_timing"],
    )
    return create_field_initializer(
        root, clock=lambda: NOW + timedelta(seconds=1), transport=transport
    ).execute(InitializeRequest.model_validate(closed_loop_document(document)))


def test_biased_async_measurements_confirm_sets_and_continue(tmp_path):
    h = UncertainHarness(tmp_path)
    for _ in range(3):
        p = h.prepare()
        assert p.payload.outcome == "accepted", p.payload.errors
        assert p.payload.data.time_basis == "bounded_uncertain_v1"
        h.services[3].execute(h.dispatch_request())
        observed = h.observe()
        assert observed.payload.outcome == "accepted", observed.payload.errors
        r = h.report()
        assert r.payload.data.cycle_status == "confirmed", r.payload.errors
        assert r.payload.data.state_semantics == "bounded_set"
        actual = r.payload.data.confirmed_state_box
        assert actual.energy_mwh.low <= h.energy <= actual.energy_mwh.high
        assert actual.energy_mwh.low < actual.energy_mwh.high
        assert r.payload.data.confirmed_state is None
        assert r.payload.data.representative_state.energy_mwh != h.energy
        assert h.query().payload.data.confirmed_state_box == actual


@pytest.mark.parametrize(
    "fault,code",
    [
        ("missing", "MEASUREMENT_EVIDENCE_REQUIRED"),
        ("calibration", "MEASUREMENT_EVIDENCE_INVALID"),
        ("too_large", "MEASUREMENT_EVIDENCE_INVALID"),
        ("expired", "MEASUREMENT_EVIDENCE_INVALID"),
        ("clock", "CLOCK_EVIDENCE_INVALID"),
        ("clock_bound", "CLOCK_EVIDENCE_INVALID"),
        ("boundary", "SAMPLE_TIME_UNRESOLVED"),
        ("settling", "TIMED_BOUNDARY_UNSETTLED"),
    ],
)
def test_bad_error_evidence_is_archived_and_cannot_confirm(tmp_path, fault, code):
    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())

    def corrupt(d):
        v = d["payload"]["device_states"][0]["p_mw"]
        e = v["uncertainty"]
        if fault == "missing":
            v["uncertainty"] = None
        elif fault == "calibration":
            e["calibration_revision"] = "unknown-calibration"
        elif fault == "too_large":
            e["absolute_error"] = 0.5
        elif fault == "expired":
            e["valid_until"] = NOW + timedelta(seconds=30)
        elif fault == "clock":
            e["timestamp"]["clock_revision"] = "unadmitted-clock"
        elif fault == "clock_bound":
            e["timestamp"]["error_seconds"] = 0.2
        elif fault == "boundary":
            v["observed_at"] += timedelta(seconds=1)
        else:
            d["payload"]["held_setpoints"][0]["held_since"] = NOW + timedelta(
                seconds=59
            )

    r = h.observe(mutate=corrupt)
    assert r.payload.errors[0].code == code, r.payload.errors
    assert h.query().payload.data.run_state == "resync_required"
    assert h.query().payload.data.last_confirmed_cycle_id is None
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM snapshot_requests WHERE snapshot_id=?",
                (h.snapshot_id,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("kind", ["load", "device"])
def test_interval_cannot_pass_by_its_center_or_be_clipped_to_a_limit(tmp_path, kind):
    d = uncertainty_document()
    s = d["payload"]["initial_snapshot"]
    if kind == "load":
        s["bus_loads"][1]["p_mw"]["value"] = 1.019
        expected = "OBSERVATION_OUTSIDE_ENVELOPE"
    else:
        s["device_states"][0]["p_mw"]["value"] = 0.448
        s["held_setpoints"][0]["p_mw"] = 0.448
        expected = "INITIAL_SAFETY_UNPROVEN"
    r = initial_result(tmp_path, d)
    assert r.payload.errors[0].code == expected, r.payload.errors


@pytest.mark.parametrize(
    "bounds,code",
    [
        ((0.9995, 1.0005), "OBSERVATION_OUTSIDE_ENVELOPE"),
        ((0.9, 1.1), "INITIAL_SAFETY_UNPROVEN"),
    ],
)
def test_root_voltage_uses_both_the_full_sensor_range_and_admitted_source_range(
    tmp_path, bounds, code
):
    d = uncertainty_document()
    d["payload"]["control_policy"]["observation_uncertainty"][
        "root_voltage_bounds_pu"
    ].update(low=bounds[0], high=bounds[1])
    r = initial_result(tmp_path, d)
    assert r.payload.errors[0].code == code, r.payload.errors


@pytest.mark.parametrize(
    "bounds,outcome",
    [
        ((0.9, 1.1), "met"),
        ((0.999999, 1.000001), "indeterminate"),
        ((1.5, 1.6), "missed"),
    ],
)
def test_task_outcome_uses_the_entire_boundary_energy_set(tmp_path, bounds, outcome):
    d = uncertainty_document()
    task = d["payload"]["planning_inputs"]["energy_task"]
    task.update(
        deadline_at=NOW + timedelta(minutes=1),
        energy_min_mwh=bounds[0],
        energy_max_mwh=bounds[1],
        required=False,
    )
    h = UncertainHarness(tmp_path, d)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    assert h.observe().payload.outcome == "accepted"
    r = h.report()
    assert r.payload.data.cycle_status == "confirmed", r.payload.errors
    assert r.payload.data.energy_task_outcome == outcome
    if outcome == "met":
        assert h.query().payload.data.energy_task_guarantee == "maintained"
        assert h.prepare().payload.outcome == "accepted"
    elif outcome == "indeterminate":
        assert (
            r.payload.data.confirmed_state_box.energy_mwh.low
            < 1
            < r.payload.data.confirmed_state_box.energy_mwh.high
        )


@pytest.mark.parametrize("offsets", [(11.9, 16.0), (13.9, 17.9)])
def test_event_ranges_crossing_application_or_skew_limits_block(tmp_path, offsets):
    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe(offsets)
    r = h.report()
    assert r.payload.errors[0].code == "EXECUTION_TIMING_UNSUPPORTED", r.payload.errors
    assert h.query().payload.data.last_confirmed_cycle_id is None


def test_uncertain_source_order_is_not_deduced_from_nominal_timestamps(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_snapshot_submitter
    from oilfield_energy.workflows.field_initialization.contracts import (
        SubmitSnapshotRequest,
    )

    h = UncertainHarness(tmp_path)
    raw = deepcopy(h.document["payload"]["initial_snapshot"])
    raw.update(
        context_id=h.initial.payload.data.context_id,
        snapshot_id="ambiguous-order",
        sequence=2,
    )
    for v in scalar_values(raw):
        v["observed_at"] = NOW + timedelta(seconds=0.25)
    r = create_snapshot_submitter(tmp_path, clock=lambda: h.now).execute(
        SubmitSnapshotRequest.model_validate(h.envelope("SubmitSnapshot", raw))
    )
    assert r.payload.errors[0].code == "SNAPSHOT_OUT_OF_ORDER", r.payload.errors


@pytest.mark.parametrize("fault", ["value", "error", "timestamp"])
def test_energy_report_must_reference_the_original_calibrated_sample(tmp_path, fault):
    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    e = next(
        d["energy_mwh"]
        for d in h.snapshot_document["payload"]["device_states"]
        if "energy_mwh" in d
    )
    energy = dict(
        method="measured_energy",
        interval_start=NOW,
        interval_end=NOW + timedelta(minutes=1),
        sample_observed_at=e["observed_at"],
        source_ref=e["source_ref"],
        energy_end_mwh=e["value"],
        uncertainty_mwh=e["uncertainty"]["absolute_error"],
    )
    if fault == "value":
        energy["energy_end_mwh"] = h.energy
    elif fault == "error":
        energy["uncertainty_mwh"] = 0.0
    else:
        energy["sample_observed_at"] = NOW + timedelta(minutes=1)
    r = h.report(energy_evidence=energy)
    assert r.payload.errors[0].code == "ENERGY_EVIDENCE_INVALID", r.payload.errors


def test_full_event_clock_evidence_is_required_for_confirmation(tmp_path):
    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    results = list(h.transport.results)
    results[0] = results[0].model_copy(update={"timestamp_uncertainty": None})
    r = h.report(results=results)
    assert r.payload.errors[0].code == "CLOCK_EVIDENCE_REQUIRED", r.payload.errors


@pytest.mark.parametrize(
    "clock", [None, {**clock_evidence(), "clock_revision": "unapproved-status-clock"}]
)
def test_discrete_device_status_cannot_silently_use_an_exact_remote_timestamp(
    tmp_path, clock
):
    d = uncertainty_document()
    d["payload"]["initial_snapshot"]["device_states"][0][
        "status_timestamp_uncertainty"
    ] = clock
    r = initial_result(tmp_path, d)
    assert r.payload.errors[0].code == (
        "CLOCK_EVIDENCE_REQUIRED" if clock is None else "CLOCK_EVIDENCE_INVALID"
    ), r.payload.errors


def test_expired_metrology_policy_cannot_initialize_a_session(tmp_path):
    d = uncertainty_document()
    d["payload"]["control_policy"]["observation_uncertainty"]["valid_until"] = (
        NOW + timedelta(seconds=0.5)
    )
    r = initial_result(tmp_path, d)
    assert r.payload.errors[0].code == "UNCERTAINTY_POLICY_EXPIRED", r.payload.errors


def reactive_document():
    d = uncertainty_document()
    d["payload"]["model_package"]["devices"][0].update(
        q_min_mvar=-0.3, q_max_mvar=0.3, q_control_enabled=True
    )
    d["payload"]["control_policy"]["bounded_timing"]["resources"][0].update(
        settled_q_error_mvar=0.001, zero_q_is_exact=True
    )
    for value in scalar_values(d["payload"]["initial_snapshot"]):
        if value["source_ref"] == "resource:wind:q":
            value["uncertainty"]["absolute_error"] = 0.001
    for m in d["payload"]["control_policy"]["observation_uncertainty"]["measurements"]:
        if m["source_ref"] == "resource:wind:q":
            m["max_absolute_error"] = 0.001
    return d


def accept_target(h):
    from oilfield_energy.bootstrap.field_initialization import (
        create_pcc_target_requester,
    )
    from oilfield_energy.workflows.field_initialization.contracts import (
        RequestPccTargetRequest,
    )
    from tests.support.field_targets import target_document

    raw = target_document(h.initial)
    raw["created_at"] = h.now
    raw["payload"]["target_p_import_mw"] = 0.8
    r = create_pcc_target_requester(h.root, clock=lambda: h.now).execute(
        RequestPccTargetRequest.model_validate(raw)
    )
    assert r.payload.outcome == "accepted", r.payload.errors
    return r


def reactive_sample(document):
    q = document["payload"]["device_states"][0]["q_mvar"]
    q["uncertainty"]["absolute_error"] = 0.001
    q["value"] += 0.0005


def test_nonzero_reactive_and_energy_ranges_survive_target_and_planning_update(
    tmp_path,
):
    from oilfield_energy.bootstrap.field_initialization import create_planning_updater
    from oilfield_energy.workflows.field_initialization.contracts import (
        UpdatePlanningRequest,
    )
    from tests.support.field_planning import update_document

    h = UncertainHarness(tmp_path, reactive_document())
    target = accept_target(h)
    for _ in range(3):
        p = h.prepare()
        assert p.payload.outcome == "accepted", p.payload.errors
        h.services[3].execute(h.dispatch_request())
        observed = h.observe(mutate=reactive_sample)
        assert observed.payload.outcome == "accepted", observed.payload.errors
        r = h.report()
        assert r.payload.data.cycle_status == "confirmed", r.payload.errors
        assert (
            r.payload.data.confirmed_state_box.q_mvar[0].low
            <= h.state["wind"][1]
            <= r.payload.data.confirmed_state_box.q_mvar[0].high
        )
        assert (
            r.payload.data.target_response_deadline_at
            == target.payload.data.response_deadline_at
        )
    update = update_document(h.document, h.initial, offset_minutes=3)
    update["created_at"] = h.now
    r = create_planning_updater(tmp_path, clock=lambda: h.now).execute(
        UpdatePlanningRequest.model_validate(update)
    )
    assert r.payload.data.activation_status == "activated", r.payload.errors
    assert (
        h.query().payload.data.target_response_deadline_at
        == target.payload.data.response_deadline_at
    )


def test_tracking_is_unproven_when_only_the_meter_center_meets_the_band(tmp_path):
    d = reactive_document()
    d["payload"]["initial_snapshot"]["pcc"]["p_import_mw"]["uncertainty"][
        "absolute_error"
    ] = 0.04
    for m in d["payload"]["control_policy"]["observation_uncertainty"]["measurements"]:
        if m["source_ref"] == "pcc:pcc:p":
            m["max_absolute_error"] = 0.04
    h = UncertainHarness(tmp_path, d)
    accept_target(h)
    for minute in range(2):
        assert h.prepare().payload.outcome == "accepted"
        h.services[3].execute(h.dispatch_request())

        def sample(raw):
            reactive_sample(raw)
            v = raw["payload"]["pcc"]["p_import_mw"]
            v["uncertainty"]["absolute_error"] = 0.04
            if minute == 1:
                v["value"] += 0.038

        observed = h.observe(mutate=sample)
        if minute == 0:
            assert observed.payload.outcome == "accepted", observed.payload.errors
            assert h.report().payload.data.cycle_status == "confirmed"
        else:
            assert any(
                e.code == "TARGET_TRACKING_UNPROVEN" for e in observed.payload.errors
            ), observed.payload.errors
            center = h.snapshot_document["payload"]["pcc"]["p_import_mw"]["value"]
            assert abs(center - 0.8) < 0.05
            assert h.query().payload.data.last_confirmed_cycle_id == "cycle-0"


def test_energy_is_aligned_to_the_boundary_without_rewriting_the_sample(tmp_path):
    from fractions import Fraction as F

    from oilfield_energy.bootstrap.field_initialization import create_execution_services
    from oilfield_energy.workflows.field_initialization.contracts import (
        QueryRunStatusRequest,
    )

    d = uncertainty_document()
    energy = d["payload"]["initial_snapshot"]["device_states"][1]["energy_mwh"]
    energy["value"] = float(F(1) - F(0.1) / F(0.95) * F(1, 10) / 3600)
    energy["uncertainty"]["absolute_error"] = 0.000001
    for m in d["payload"]["control_policy"]["observation_uncertainty"]["measurements"]:
        if m["source_ref"] == energy["source_ref"]:
            m["max_absolute_error"] = 0.000001
    r = initial_result(tmp_path, d)
    assert r.payload.outcome == "accepted", r.payload.errors
    query = QueryRunStatusRequest.model_validate(
        dict(
            interface_version=r.interface_version,
            operation="QueryRunStatus",
            message_id="aligned-query",
            correlation_id=None,
            microgrid_id=r.microgrid_id,
            run_id=r.run_id,
            created_at=NOW + timedelta(seconds=1),
            payload={},
        )
    )
    status = create_execution_services(
        tmp_path, clock=lambda: NOW + timedelta(seconds=1)
    )[1].execute(query)
    assert energy["value"] + 0.000001 < 1.0
    assert (
        status.payload.data.confirmed_state_box.energy_mwh.low
        <= 1.0
        <= status.payload.data.confirmed_state_box.energy_mwh.high
    )
    assert energy["observed_at"] == NOW + timedelta(seconds=0.2)


def recovery_request(h, *, fence=-3.0):
    from oilfield_energy.workflows.field_initialization.contracts import (
        PrepareResynchronizationRequest,
    )

    batch = h.transport.sent[-1]
    authority = deepcopy(h.document["payload"]["authority"])
    authority["epoch"] += 1
    return PrepareResynchronizationRequest.model_validate(
        h.envelope(
            "PrepareResynchronization",
            dict(
                context_id=h.initial.payload.data.context_id,
                resynchronization_id="uncertain-recovery",
                pending_batch_id=batch.evidence.command_batch_id,
                snapshot=h.snapshot_document["payload"],
                planning_inputs=h.document["payload"]["planning_inputs"],
                authority=authority,
                quiescence=dict(
                    command_batch_id=batch.evidence.command_batch_id,
                    previous_authority=batch.evidence.request.payload.authority,
                    fenced_at=NOW + timedelta(minutes=h.minute, seconds=fence),
                    timestamp_uncertainty=clock_evidence(),
                    source_ref="analytic-supervisor:fence",
                    settlements=[
                        dict(
                            command_id=r.command_id,
                            resource_id=r.resource_id,
                            disposition="applied",
                            terminal_at=r.applied_at,
                            timestamp_uncertainty=clock_evidence(),
                            source_ref="analytic:terminal",
                        )
                        for r in h.transport.results
                    ],
                ),
                reason="operator reconciled",
            ),
        )
    )


def test_uncertain_recovery_commits_a_new_set_and_continues(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_recovery_services
    from oilfield_energy.workflows.field_initialization.contracts import (
        ResumeRunRequest,
    )
    from tests.support.field_recovery import SoftwareRecoveryVerifier

    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    h.stop()
    prepare, resume = create_recovery_services(
        tmp_path,
        clock=lambda: h.now,
        transport=h.transport,
        verifier=SoftwareRecoveryVerifier(),
    )
    proposal = recovery_request(h)
    candidate = prepare.execute(proposal)
    assert candidate.payload.outcome == "pending", candidate.payload.errors
    h.advance(1)
    request = ResumeRunRequest.model_validate(
        h.envelope(
            "ResumeRun",
            dict(
                context_id=proposal.payload.context_id,
                resynchronization_id=proposal.payload.resynchronization_id,
                proposed_context_id=candidate.payload.data.proposed_context_id,
                authority=proposal.payload.authority,
            ),
        )
    )
    resumed = resume.execute(request)
    assert resumed.payload.data.run_state == "ready", resumed.payload.errors
    assert resumed.payload.data.confirmed_state is None
    assert resumed.payload.data.last_confirmed_cycle_id is None
    assert resume.execute(request) == resumed
    h.initial = h.initial.model_copy(
        update={
            "payload": h.initial.payload.model_copy(
                update={
                    "data": h.initial.payload.data.model_copy(
                        update={"context_id": resumed.payload.data.context_id}
                    )
                }
            )
        }
    )
    h.document["payload"]["authority"] = proposal.payload.authority.model_dump(
        mode="json"
    )
    assert h.prepare().payload.outcome == "accepted"
    h.services[3].execute(h.dispatch_request())
    assert h.observe().payload.outcome == "accepted"
    assert h.report().payload.data.last_confirmed_cycle_id == "cycle-1"


@pytest.mark.parametrize("fault", ["fence", "terminal"])
def test_recovery_needs_proven_event_order_for_the_full_time_ranges(tmp_path, fault):
    from oilfield_energy.bootstrap.field_initialization import create_recovery_services
    from tests.support.field_recovery import SoftwareRecoveryVerifier

    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    h.stop()
    proposal = recovery_request(h, fence=-0.05 if fault == "fence" else -3.0)
    if fault == "terminal":
        raw = proposal.model_dump()
        raw["payload"]["quiescence"]["settlements"][0]["terminal_at"] = NOW + timedelta(
            seconds=56.95
        )
        proposal = type(proposal).model_validate(raw)
    r = create_recovery_services(
        tmp_path,
        clock=lambda: h.now,
        transport=h.transport,
        verifier=SoftwareRecoveryVerifier(),
    )[0].execute(proposal)
    assert r.payload.errors[0].code == "RECOVERY_FENCE_INVALID", r.payload.errors
    assert h.query().payload.data.run_state == "stopped"


@pytest.mark.parametrize("scope", ["metrology", "timing"])
def test_policy_content_cannot_be_changed_under_an_approved_revision(tmp_path, scope):
    from oilfield_energy.bootstrap.field_initialization import create_field_initializer
    from oilfield_energy.workflows.field_initialization.contracts import (
        InitializeRequest,
    )
    from tests.support.field_timing import closed_loop_document

    d = uncertainty_document()
    policy = d["payload"]["control_policy"]
    transport = UncertainTransport(
        lambda: NOW + timedelta(seconds=1),
        lambda _: None,
        policy["observation_uncertainty"],
        policy["bounded_timing"],
    )
    if scope == "metrology":
        policy["observation_uncertainty"]["measurements"][0]["max_absolute_error"] = (
            0.0001
        )
    else:
        policy["bounded_timing"]["resources"][0]["settled_p_error_mw"] = 0.0
    r = create_field_initializer(
        tmp_path, clock=lambda: NOW + timedelta(seconds=1), transport=transport
    ).execute(InitializeRequest.model_validate(closed_loop_document(d)))
    assert r.payload.errors[0].code == (
        "OBSERVATION_POLICY_UNVERIFIED"
        if scope == "metrology"
        else "TIMING_POLICY_UNVERIFIED"
    ), r.payload.errors


def test_a_report_cannot_claim_a_smaller_error_than_the_approved_calibration(tmp_path):
    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())

    def reduced(raw):
        raw["payload"]["device_states"][0]["p_mw"]["uncertainty"]["absolute_error"] = (
            0.0
        )

    r = h.observe(mutate=reduced)
    assert r.payload.errors[0].code == "MEASUREMENT_EVIDENCE_INVALID", r.payload.errors


@pytest.mark.parametrize("admitted", [False, True])
def test_estimates_keep_their_kind_and_need_explicit_error_admission(
    tmp_path, admitted
):
    d = uncertainty_document()
    v = d["payload"]["initial_snapshot"]["device_states"][0]["p_mw"]
    v["source_kind"] = "estimated"
    if admitted:
        for m in d["payload"]["control_policy"]["observation_uncertainty"][
            "measurements"
        ]:
            if m["source_ref"] == v["source_ref"]:
                m["source_kind"] = "estimated"
    r = initial_result(tmp_path, d)
    assert r.payload.outcome == ("accepted" if admitted else "rejected"), (
        r.payload.errors
    )
    if not admitted:
        assert r.payload.errors[0].code == "MEASUREMENT_EVIDENCE_INVALID"


def test_required_task_cannot_use_only_the_representative_energy(tmp_path):
    d = uncertainty_document()
    d["payload"]["planning_inputs"]["energy_task"].update(
        deadline_at=NOW + timedelta(minutes=1),
        energy_min_mwh=1.0005,
        energy_max_mwh=1.0015,
        required=True,
    )
    r = initial_result(tmp_path, d)
    assert r.payload.outcome == "rejected"
    assert r.payload.errors[0].code in ("TASK_UNPROVEN", "TIMED_TASK_UNPROVEN"), (
        r.payload.errors
    )


def test_consistent_observations_do_not_replace_missing_device_adoption(tmp_path):
    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    delivery_only = h.transport.results
    h.observe()
    r = h.report(results=delivery_only)
    assert r.payload.errors[0].code == "PARTIAL_OR_UNKNOWN_EXECUTION", r.payload.errors
    assert h.query().payload.data.last_confirmed_cycle_id is None


def test_failed_set_commit_rolls_back_and_replays_once_after_restart(
    tmp_path, monkeypatch
):
    import oilfield_energy.workflows.field_initialization.adapters.execution_storage as storage

    h = UncertainHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    observation = h.observe()
    request = h.report_request()
    original = storage.save_ledger

    def unavailable(db, run, ledger):
        original(db, run, ledger)
        raise sqlite3.OperationalError("injected bounded-state persistence failure")

    with monkeypatch.context() as m:
        m.setattr(storage, "save_ledger", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            h.services[0].execute(request)
    assert h.query().payload.data.last_confirmed_cycle_id is None
    completed = h.services[0].execute(request)
    assert completed.payload.data.cycle_status == "confirmed", completed.payload.errors
    assert h.services[0].execute(request) == completed
    observed = observation.payload.data.observed_box.energy_mwh
    physical = h.prepared.payload.data.timed_proof.endpoint.energy_mwh
    confirmed = completed.payload.data.confirmed_state_box.energy_mwh
    assert confirmed.low == max(observed.low, physical.low)
    assert confirmed.high == min(observed.high, physical.high)
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM execution_requests WHERE message_id=?",
                (request.message_id,),
            ).fetchone()[0]
            == 1
        )


def test_estimated_energy_cannot_be_labeled_as_measured_energy(tmp_path):
    d = uncertainty_document()
    d["payload"]["initial_snapshot"]["device_states"][1]["energy_mwh"][
        "source_kind"
    ] = "estimated"
    for m in d["payload"]["control_policy"]["observation_uncertainty"]["measurements"]:
        if m["source_ref"] == "resource:storage:energy":
            m["source_kind"] = "estimated"
    h = UncertainHarness(tmp_path, d)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    request = h.report_request()
    assert request.payload.energy_evidence.method == "bounded_estimate"
    assert h.services[0].execute(request).payload.data.cycle_status == "confirmed"
    raw = request.model_dump()
    raw["message_id"] = "mislabeled-estimate-history"
    raw["payload"]["energy_evidence"]["method"] = "measured_energy"
    historical = h.services[0].execute(type(request).model_validate(raw))
    assert historical.payload.errors[0].code == "ENERGY_EVIDENCE_INVALID"
    assert h.query().payload.data.run_state == "ready"
    assert h.prepare().payload.outcome == "accepted"
    h.services[3].execute(h.dispatch_request())
    h.observe()
    next_request = h.report_request()
    raw = next_request.model_dump()
    raw["payload"]["energy_evidence"]["method"] = "measured_energy"
    r = h.services[0].execute(type(request).model_validate(raw))
    assert r.payload.errors[0].code == "ENERGY_EVIDENCE_INVALID", r.payload.errors

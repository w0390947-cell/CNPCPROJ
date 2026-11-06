"""One-step analytic preparation, proof boundaries and durable atomic history."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest

from oilfield_energy.bootstrap.field_initialization import (
    create_control_cycle_preparer,
    create_field_initializer,
    create_pcc_target_requester,
    create_planning_updater,
    create_snapshot_submitter,
)
from oilfield_energy.workflows.field_initialization.contracts import (
    ControlCycleEvidence,
    InitializationEvidence,
    InitializeRequest,
    PrepareControlCycleRequest,
    RequestPccTargetRequest,
    SubmitSnapshotRequest,
    UpdatePlanningRequest,
)
from oilfield_energy.workflows.single_execution.api import (
    UnifiedSingleController,
    prepare_certified_cycle,
)
from tests.support.field_cycles import cycle_document
from tests.support.field_initialization import NOW
from tests.support.field_planning import initialized, update_document
from tests.support.field_snapshots import snapshot_document
from tests.support.field_targets import target_case, target_document


def prepare(root, document, now=NOW):
    return create_control_cycle_preparer(root, clock=lambda: now).execute(
        PrepareControlCycleRequest.model_validate(document)
    )


def rows(root, sql):
    with sqlite3.connect(root / "sessions.sqlite3") as db:
        return db.execute(sql).fetchall()


def test_shadow_prediction_preserves_confirmed_state_and_all_other_heads(
    tmp_path, monkeypatch
):
    original, init = initialized(tmp_path)
    before = rows(tmp_path, "SELECT evidence FROM initialization_sessions")

    def forbidden(*args, **kwargs):
        pytest.fail("Field preparation must not execute the simulation controller")

    monkeypatch.setattr(UnifiedSingleController, "advance", forbidden)
    result = prepare(tmp_path, cycle_document(original, init))
    assert result.payload.outcome == "accepted", result
    data = result.payload.data
    assert (
        data.decision == "shadow_only"
        and not data.dispatch_enabled
        and not data.execution_confirmed
    )
    assert data.dispatch_deadline_at is None and data.feedback_due_at is None
    assert all(c.q_setpoint_mvar is None for c in data.commands)
    assert data.model_check.guaranteed_until == NOW + timedelta(minutes=1)
    assert rows(tmp_path, "SELECT evidence FROM initialization_sessions") == before
    for table in (
        "planning_heads",
        "pcc_target_heads",
        "snapshot_heads",
        "observation_barriers",
    ):
        assert rows(tmp_path, f"SELECT * FROM {table}") == []
    evidence = ControlCycleEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM cycle_requests")[0][0]
    )
    current = InitializationEvidence.model_validate_json(before[0][0])
    p = evidence.prepared.command.p_mw[
        next(
            i
            for i, r in enumerate(current.domain.devices.resources)
            if r.resource_id == "storage"
        )
    ]
    storage = next(
        d
        for d in original["payload"]["model_package"]["devices"]
        if d["kind"] == "storage"
    )
    # Independent minute-energy oracle (discharge-positive convention).
    delta = (
        -p / storage["eta_discharge"] / 60
        if p >= 0
        else -p * storage["eta_charge"] / 60
    )
    assert data.predicted_endpoint.storage_energy_mwh == pytest.approx(
        current.initial.energy_mwh + delta
    )


def test_shared_preparation_and_original_controller_commit_match_one_step(tmp_path):
    original, init = initialized(tmp_path)
    result = prepare(tmp_path, cycle_document(original, init))
    assert result.payload.outcome == "accepted", result
    e = ControlCycleEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM cycle_requests")[0][0]
    )
    current = InitializationEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM initialization_sessions")[0][0]
    )
    controller = UnifiedSingleController(e.domain, current.initial)
    expected = prepare_certified_cycle(
        e.domain,
        controller.state,
        controller.expected,
        0,
        controller.plans[0],
        e.observed,
    )
    assert not controller.steps and controller.state == current.initial
    row = controller.advance(
        e.observed, context_id=e.domain.context_id, target=(1.0, 0.0), proposal=None
    )
    assert row.command == expected.command == e.prepared.command
    assert row.after == expected.predicted_endpoint == e.prepared.predicted_endpoint
    assert controller.expected == expected.expected_next


def test_target_feedback_preserves_original_obligations(tmp_path):
    original, init = initialized(tmp_path, target_case())
    target = create_pcc_target_requester(tmp_path, clock=lambda: NOW).execute(
        RequestPccTargetRequest.model_validate(target_document(init))
    )
    assert target.payload.outcome == "accepted", target
    result = prepare(tmp_path, cycle_document(original, init))
    assert result.payload.outcome == "accepted", result
    d = result.payload.data
    assert d.strategy_source == "retained_feedback"
    assert d.target_id == "external-1"
    assert d.target_response_deadline_at == NOW + timedelta(minutes=2)
    assert d.target_guaranteed_until == NOW + timedelta(minutes=5)
    assert d.energy_task_deadline_at == NOW + timedelta(minutes=120)
    assert d.commands[0].q_setpoint_mvar is not None


@pytest.mark.parametrize(
    "change,code",
    [
        ({"context_id": "other"}, "CONTEXT_CHANGED"),
        ({"snapshot_id": "missing"}, "SNAPSHOT_NOT_CURRENT"),
        (
            {"cycle_end_at": (NOW + timedelta(seconds=30)).isoformat()},
            "CYCLE_TIME_INVALID",
        ),
        (
            {"cycle_start_at": (NOW + timedelta(minutes=1)).isoformat()},
            "CONFIRMED_STATE_REQUIRED",
        ),
    ],
)
def test_invalid_cycle_is_withheld_without_commands(tmp_path, change, code):
    original, init = initialized(tmp_path)
    doc = cycle_document(original, init)
    doc["payload"].update(change)
    result = prepare(tmp_path, doc)
    assert result.payload.errors[0].code == code
    d = result.payload.data
    assert (
        d.decision == "withheld"
        and d.command_batch_id is None
        and d.predicted_endpoint is None
        and not d.commands
    )
    assert not rows(tmp_path, "SELECT * FROM prepared_cycle_heads")


def test_authority_change_and_expired_initial_state(tmp_path):
    original, init = initialized(tmp_path)
    d = cycle_document(original, init)
    d["payload"]["authority"]["epoch"] += 1
    assert prepare(tmp_path, d).payload.errors[0].code == "AUTHORITY_CHANGED"
    d = cycle_document(original, init)
    d["message_id"] = "expired"
    d["payload"]["cycle_id"] = "expired"
    assert (
        prepare(tmp_path, d, NOW + timedelta(minutes=1)).payload.errors[0].code
        == "CONFIRMED_STATE_REQUIRED"
    )


def test_idempotency_restart_cycle_identity_and_single_pending_batch(tmp_path):
    original, init = initialized(tmp_path)
    d = cycle_document(original, init)
    first = prepare(tmp_path, d)
    assert prepare(tmp_path, d, NOW + timedelta(hours=1)) == first
    d["message_id"] = "retry-new-envelope"
    replay = prepare(tmp_path, d)
    assert replay.payload == first.payload and replay.correlation_id == d["message_id"]
    d["payload"]["snapshot_id"] = "different"
    d["message_id"] = "conflict"
    assert prepare(tmp_path, d).payload.errors[0].code == "CYCLE_ID_CONFLICT"
    d = cycle_document(original, init)
    d["payload"]["cycle_id"] = "cycle-2"
    d["message_id"] = "cycle-2"
    assert prepare(tmp_path, d).payload.errors[0].code == "CYCLE_ALREADY_PREPARED"
    assert len(rows(tmp_path, "SELECT * FROM prepared_cycle_heads")) == 1


def test_concurrent_preparation_has_one_batch(tmp_path):
    original, init = initialized(tmp_path)
    d = cycle_document(original, init)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(tmp_path, d), range(2)))
    assert results[0] == results[1]
    assert results[0].payload.outcome == "accepted"
    assert len(rows(tmp_path, "SELECT * FROM cycle_requests")) == 1


@pytest.mark.parametrize("operation", ["snapshot", "target", "planning"])
def test_changed_inputs_invalidate_shadow_pointer_without_rewriting_history(
    tmp_path, operation
):
    original, init = initialized(tmp_path, target_case())
    d = cycle_document(original, init)
    first = prepare(tmp_path, d)
    assert first.payload.outcome == "accepted"
    history = rows(tmp_path, "SELECT response, evidence FROM cycle_requests")
    if operation == "snapshot":
        doc = snapshot_document(original, init)
        response = create_snapshot_submitter(tmp_path, clock=lambda: NOW).execute(
            SubmitSnapshotRequest.model_validate(doc)
        )
    elif operation == "target":
        response = create_pcc_target_requester(tmp_path, clock=lambda: NOW).execute(
            RequestPccTargetRequest.model_validate(target_document(init))
        )
    else:
        response = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
            UpdatePlanningRequest.model_validate(update_document(original, init))
        )
        assert response.payload.data.activation_status == "activated"
    assert response.payload.outcome == "accepted", response
    assert not rows(tmp_path, "SELECT * FROM prepared_cycle_heads")
    assert rows(tmp_path, "SELECT response, evidence FROM cycle_requests") == history
    assert len(rows(tmp_path, "SELECT * FROM cycle_invalidations")) == 1
    assert prepare(tmp_path, d) == first
    assert not rows(tmp_path, "SELECT * FROM prepared_cycle_heads")
    d["message_id"] = "historical-new-envelope"
    assert prepare(tmp_path, d).payload == first.payload
    assert not rows(tmp_path, "SELECT * FROM prepared_cycle_heads")


def test_fresh_same_boundary_snapshot_required_and_next_minute_blocks(tmp_path):
    original, init = initialized(tmp_path)
    snap = snapshot_document(original, init)
    service = create_snapshot_submitter(tmp_path, clock=lambda: NOW)
    assert (
        service.execute(SubmitSnapshotRequest.model_validate(snap)).payload.outcome
        == "accepted"
    )
    d = cycle_document(original, init)
    assert prepare(tmp_path, d).payload.errors[0].code == "SNAPSHOT_NOT_CURRENT"
    d["message_id"] = "latest"
    d["payload"].update(cycle_id="latest", snapshot_id=snap["payload"]["snapshot_id"])
    assert prepare(tmp_path, d).payload.outcome == "accepted"
    later = snapshot_document(original, init, minute=1, sequence=3)
    response = create_snapshot_submitter(
        tmp_path, clock=lambda: NOW + timedelta(minutes=1)
    ).execute(SubmitSnapshotRequest.model_validate(later))
    assert response.payload.data.resync_required
    d["message_id"] = "next-minute"
    d["payload"]["cycle_id"] = "next-minute"
    assert (
        prepare(tmp_path, d, NOW + timedelta(minutes=1)).payload.errors[0].code
        == "RESYNC_REQUIRED"
    )


@pytest.mark.parametrize("operation", ["initialize", "planning", "target", "snapshot"])
def test_message_namespace_collision_both_directions(tmp_path, operation):
    original, init = initialized(tmp_path, target_case())
    d = cycle_document(original, init)
    assert prepare(tmp_path, d).payload.outcome == "accepted"
    doc, service, model = {
        "initialize": (deepcopy(original), create_field_initializer, InitializeRequest),
        "planning": (
            update_document(original, init),
            create_planning_updater,
            UpdatePlanningRequest,
        ),
        "target": (
            target_document(init),
            create_pcc_target_requester,
            RequestPccTargetRequest,
        ),
        "snapshot": (
            snapshot_document(original, init),
            create_snapshot_submitter,
            SubmitSnapshotRequest,
        ),
    }[operation]
    doc["message_id"] = d["message_id"]
    assert (
        service(tmp_path, clock=lambda: NOW)
        .execute(model.model_validate(doc))
        .payload.errors[0]
        .code
        == "DUPLICATE_ID_CONFLICT"
    )
    d["message_id"] = original["message_id"]
    assert prepare(tmp_path, d).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"


def test_storage_failure_rolls_back_batch_and_response(tmp_path):
    original, init = initialized(tmp_path)
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_batch BEFORE INSERT ON prepared_cycle_heads BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        prepare(tmp_path, cycle_document(original, init))
    assert not rows(tmp_path, "SELECT * FROM cycle_requests")
    assert not rows(tmp_path, "SELECT * FROM prepared_cycle_heads")


@pytest.mark.parametrize("failure", ["budget", "clock", "proof"])
def test_failed_preparation_does_not_commit_prediction(tmp_path, monkeypatch, failure):
    from oilfield_energy.workflows.field_initialization.api import PrepareControlCycle
    from oilfield_energy.workflows.field_initialization.adapters.cycle_storage import (
        SqliteControlCycles,
    )
    from oilfield_energy.workflows.field_initialization import cycle_preparation

    original, init = initialized(tmp_path)
    times = iter([NOW, NOW + timedelta(minutes=1)])
    elapsed = iter([0.0, 31.0])
    if failure == "proof":

        def unproven(*args, **kwargs):
            raise ValueError("test AC path cannot be proved")

        monkeypatch.setattr(cycle_preparation, "prepare_certified_cycle", unproven)
    service = PrepareControlCycle(
        SqliteControlCycles(tmp_path / "sessions.sqlite3"),
        (lambda: next(times, NOW + timedelta(minutes=1)))
        if failure == "clock"
        else (lambda: NOW),
        (lambda: next(elapsed)) if failure == "budget" else (lambda: 0.0),
    )
    result = service.execute(
        PrepareControlCycleRequest.model_validate(cycle_document(original, init))
    )
    assert (
        result.payload.errors[0].code
        == {
            "budget": "COMPUTE_BUDGET_EXCEEDED",
            "clock": "CONFIRMED_STATE_REQUIRED",
            "proof": "CYCLE_UNPROVEN",
        }[failure]
    )
    assert not rows(tmp_path, "SELECT * FROM prepared_cycle_heads")
    assert rows(tmp_path, "SELECT evidence FROM cycle_requests") == [(None,)]


def test_staged_planning_and_rejected_target_do_not_invalidate(tmp_path):
    original, init = initialized(tmp_path)
    assert (
        prepare(tmp_path, cycle_document(original, init)).payload.outcome == "accepted"
    )
    head = rows(tmp_path, "SELECT * FROM prepared_cycle_heads")
    response = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(
            update_document(original, init, offset_minutes=15)
        )
    )
    assert response.payload.data.activation_status == "staged"
    d = target_document(init)
    d["payload"]["target_p_import_mw"] = 1000.0
    assert (
        create_pcc_target_requester(tmp_path, clock=lambda: NOW)
        .execute(RequestPccTargetRequest.model_validate(d))
        .payload.outcome
        == "rejected"
    )
    assert rows(tmp_path, "SELECT * FROM prepared_cycle_heads") == head
    assert not rows(tmp_path, "SELECT * FROM cycle_invalidations")


def test_invalidation_and_snapshot_are_one_transaction(tmp_path):
    original, init = initialized(tmp_path)
    assert (
        prepare(tmp_path, cycle_document(original, init)).payload.outcome == "accepted"
    )
    head = rows(tmp_path, "SELECT * FROM prepared_cycle_heads")
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_invalidation BEFORE INSERT ON cycle_invalidations BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        create_snapshot_submitter(tmp_path, clock=lambda: NOW).execute(
            SubmitSnapshotRequest.model_validate(snapshot_document(original, init))
        )
    assert rows(tmp_path, "SELECT * FROM prepared_cycle_heads") == head
    assert not rows(tmp_path, "SELECT * FROM snapshot_requests")
    assert not rows(tmp_path, "SELECT * FROM snapshot_heads")


def test_older_database_gets_additive_cycle_tables(tmp_path):
    original, init = initialized(tmp_path)
    before = rows(tmp_path, "SELECT * FROM initialization_sessions")
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        for table in ("cycle_requests", "prepared_cycle_heads", "cycle_invalidations"):
            db.execute(f"DROP TABLE {table}")
    assert (
        prepare(tmp_path, cycle_document(original, init)).payload.outcome == "accepted"
    )
    assert rows(tmp_path, "SELECT * FROM initialization_sessions") == before


def test_reprepare_after_new_snapshot_needs_new_cycle_identity(tmp_path):
    original, init = initialized(tmp_path)
    d = cycle_document(original, init)
    first = prepare(tmp_path, d)
    snap = snapshot_document(original, init)
    create_snapshot_submitter(tmp_path, clock=lambda: NOW).execute(
        SubmitSnapshotRequest.model_validate(snap)
    )
    d["message_id"] = "second"
    d["payload"].update(cycle_id="second", snapshot_id=snap["payload"]["snapshot_id"])
    second = prepare(tmp_path, d)
    assert second.payload.outcome == "accepted", second
    assert second.payload.data.command_batch_id != first.payload.data.command_batch_id
    assert rows(tmp_path, "SELECT batch_id FROM prepared_cycle_heads") == [
        (second.payload.data.command_batch_id,)
    ]


def test_target_shared_prediction_matches_single_controller_admission(tmp_path):
    original, init = initialized(tmp_path, target_case())
    create_pcc_target_requester(tmp_path, clock=lambda: NOW).execute(
        RequestPccTargetRequest.model_validate(target_document(init))
    )
    assert (
        prepare(tmp_path, cycle_document(original, init)).payload.outcome == "accepted"
    )
    e = ControlCycleEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM cycle_requests")[0][0]
    )
    current = InitializationEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM initialization_sessions")[0][0]
    )
    # Controller target admission uses the domain response_steps; field requests
    # carry their own absolute deadline, so align this isolated regression fixture.
    domain = e.domain.model_copy(update={"response_steps": 2})
    controller = UnifiedSingleController(domain, current.initial)
    row = controller.advance(
        e.observed,
        context_id=domain.context_id,
        target=(1.0, 0.0),
        proposal=None,
        target_plan=e.plan,
        target_changed=True,
    )
    assert (
        row.command == e.prepared.command and row.after == e.prepared.predicted_endpoint
    )
    assert controller.expected == e.prepared.expected_next


@pytest.mark.parametrize("bounds,sign", [((1.01, 1.02), -1), ((0.98, 0.99), 1)])
def test_energy_return_prediction_changes_energy_without_confirming_it(
    tmp_path, bounds, sign
):
    from tests.support.field_initialization import request_document

    doc = request_document()
    doc["payload"]["planning_inputs"]["energy_task"].update(
        energy_min_mwh=bounds[0], energy_max_mwh=bounds[1]
    )
    original, init = initialized(tmp_path, doc)
    result = prepare(tmp_path, cycle_document(original, init))
    assert result.payload.outcome == "accepted", result
    data = result.payload.data
    assert data.energy_feedback
    command = next(c for c in data.commands if c.resource_id == "storage")
    p = command.p_setpoint_mw
    assert p * sign > 0
    expected = 1.0 - (p / 0.95 if p > 0 else p * 0.95) / 60
    assert data.predicted_endpoint.storage_energy_mwh == pytest.approx(
        expected, abs=1e-12
    )
    current = InitializationEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM initialization_sessions")[0][0]
    )
    assert current.initial.energy_mwh == 1.0
    evidence = ControlCycleEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM cycle_requests")[0][0]
    )
    controller = UnifiedSingleController(current.domain, current.initial)
    row = controller.advance(
        evidence.observed,
        context_id=current.domain.context_id,
        target=(1.0, 0.0),
        proposal=None,
    )
    assert row.after == evidence.prepared.predicted_endpoint
    assert controller.expected == evidence.prepared.expected_next


@pytest.mark.parametrize("problem", ["target_context", "target_expiry", "input_expiry"])
def test_stale_retained_evidence_cannot_be_used_as_fallback(tmp_path, problem):
    from oilfield_energy.workflows.field_initialization.contracts import (
        PccTargetEvidence,
        InitializationFailure,
    )
    from oilfield_energy.workflows.field_initialization.cycle_preparation import (
        prepare_cycle_evidence,
    )

    original, init = initialized(tmp_path, target_case())
    create_pcc_target_requester(tmp_path, clock=lambda: NOW).execute(
        RequestPccTargetRequest.model_validate(target_document(init))
    )
    current = InitializationEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM initialization_sessions")[0][0]
    )
    target = PccTargetEvidence.model_validate_json(
        rows(tmp_path, "SELECT evidence FROM pcc_target_heads")[0][0]
    )
    if problem == "target_context":
        target = target.model_copy(update={"context_id": "old-context"})
    elif problem == "target_expiry":
        target = target.model_copy(
            update={"guaranteed_until": NOW + timedelta(seconds=30)}
        )
    else:
        current = current.model_copy(
            update={"valid_until": NOW + timedelta(seconds=30)}
        )
    with pytest.raises(InitializationFailure) as error:
        prepare_cycle_evidence(
            PrepareControlCycleRequest.model_validate(cycle_document(original, init)),
            current,
            target,
            None,
            "batch",
            NOW,
        )
    assert error.value.error.code == (
        "FORECAST_COVERAGE_INSUFFICIENT"
        if problem == "input_expiry"
        else "TARGET_UNAVAILABLE"
    )


def test_no_authorized_axes_cannot_produce_an_empty_batch(tmp_path):
    from tests.support.field_initialization import request_document

    doc = request_document()
    for device in doc["payload"]["model_package"]["devices"]:
        device.update(p_control_enabled=False, p_min_mw=0.0, p_max_mw=0.0)
    for point in doc["payload"]["planning_inputs"]["forecast_points"]:
        point["renewables"][0].update(
            available_forecast_mw=0.0, minimum_available_mw=0.0
        )
    doc["payload"]["initial_snapshot"]["renewable_availability"][0]["available_p_mw"][
        "value"
    ] = 0.0
    original, init = initialized(tmp_path, doc)
    response = prepare(tmp_path, cycle_document(original, init))
    assert response.payload.errors[0].code == "NO_AUTHORIZED_COMMANDS"
    assert (
        not response.payload.data.commands
        and response.payload.data.command_batch_id is None
    )

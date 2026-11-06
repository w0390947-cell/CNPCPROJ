"""Real small admission proofs plus transaction and staging checks; no simulation."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest

from oilfield_energy.bootstrap.field_initialization import (
    create_field_initializer,
    create_planning_updater,
)
from oilfield_energy.workflows.field_initialization.contracts import (
    InitializationEvidence,
    InitializeRequest,
    UpdatePlanningRequest,
)
from tests.support.field_initialization import NOW, request_document
from tests.support.field_planning import initialized, update_document


def execute(root, document, now=NOW):
    return create_planning_updater(root, clock=lambda: now).execute(
        UpdatePlanningRequest.model_validate(document)
    )


def state(root):
    with sqlite3.connect(root / "sessions.sqlite3") as db:
        original = db.execute(
            "SELECT evidence FROM initialization_sessions"
        ).fetchone()[0]
        active = db.execute(
            "SELECT u.evidence FROM planning_heads h JOIN planning_updates u ON h.microgrid_id=u.microgrid_id AND h.message_id=u.message_id"
        ).fetchone()
        return original, active[0] if active else None


def test_activation_preserves_state_task_and_original_history(tmp_path):
    doc, init = initialized(tmp_path)
    before = state(tmp_path)
    request = update_document(doc, init)
    result = execute(tmp_path, request)
    data = result.payload.data
    assert data.activation_status == "activated", result
    assert result.run_id == init.run_id
    assert data.new_context_id != init.payload.data.context_id
    assert data.energy_task_guarantee == "maintained" and not data.dispatch_enabled
    original, active = state(tmp_path)
    assert original == before[0] and active is not None
    proof = InitializationEvidence.model_validate_json(active)
    old = InitializationEvidence.model_validate_json(original)
    assert proof.initial == old.initial
    assert (
        proof.request.payload.initial_snapshot == old.request.payload.initial_snapshot
    )
    assert proof.domain.energy_task == old.domain.energy_task
    assert (
        proof.request.payload.planning_inputs.energy_task
        == old.request.payload.planning_inputs.energy_task
    )
    assert proof.domain.economic_rates.loss_cny_per_mwh == 20.0
    assert proof.plan.task_continuation is not None
    assert proof.domain.context_id == data.new_context_id
    assert execute(tmp_path, request, NOW + timedelta(days=1)) == result
    # Original initialization response remains historical after an activation.
    assert (
        create_field_initializer(tmp_path, clock=lambda: NOW).execute(
            InitializeRequest.model_validate(doc)
        )
        == init
    )


def test_future_inputs_are_staged_without_proof_or_state_change(tmp_path, monkeypatch):
    import oilfield_energy.workflows.field_initialization.application_updates as updates

    doc, init = initialized(tmp_path)
    before = state(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Staging must not certify from a hypothetical future state")

    monkeypatch.setattr(updates, "certify_conditions", forbidden)
    d = update_document(doc, init, offset_minutes=15)
    result = execute(tmp_path, d)
    data = result.payload.data
    assert data.activation_status == "staged"
    assert data.new_context_id is None and data.evidence_ref is None
    assert data.energy_task_guarantee == "not_assessed"
    assert data.reason.startswith("FUTURE_BOUNDARY")
    assert state(tmp_path) == before
    # Passing the effective time does not activate an old stored response.
    assert execute(tmp_path, d, NOW + timedelta(minutes=15)) == result
    assert state(tmp_path) == before


@pytest.mark.parametrize(
    "seconds,offset,reason",
    [
        (31, 0, "STALE_CONFIRMED_STATE"),
        (61, 0, "CONFIRMED_STATE_REQUIRED"),
        (900, 15, "CONFIRMED_STATE_REQUIRED"),
    ],
)
def test_old_snapshot_cannot_be_reused_as_current_state(
    tmp_path, seconds, offset, reason
):
    original = request_document()
    if seconds == 61:
        original["payload"]["control_policy"]["telemetry_max_age_seconds"] = 300.0
    doc, init = initialized(tmp_path, original)
    d = update_document(doc, init, offset_minutes=offset)
    now = NOW + timedelta(seconds=seconds)
    d["created_at"] = now.isoformat()
    result = execute(tmp_path, d, now)
    assert result.payload.data.activation_status == "staged", result
    assert result.payload.data.reason.startswith(reason)
    assert state(tmp_path)[1] is None


@pytest.mark.parametrize(
    "change", ["remove", "id", "deadline", "bounds", "required", "source", "add"]
)
def test_task_changes_are_rejected_without_affecting_current_context(tmp_path, change):
    original = request_document()
    if change == "add":
        original["payload"]["planning_inputs"]["energy_task"] = None
    doc, init = initialized(tmp_path, original)
    d = update_document(doc, init)
    task = d["payload"]["energy_task"]
    if change == "remove":
        d["payload"]["energy_task"] = None
    elif change == "add":
        d["payload"]["energy_task"] = request_document()["payload"]["planning_inputs"][
            "energy_task"
        ]
    elif change == "id":
        task["task_id"] = "replacement"
    elif change == "deadline":
        task["deadline_at"] = (NOW + timedelta(hours=3)).isoformat()
    elif change == "bounds":
        task["energy_min_mwh"] = 0.9
    elif change == "required":
        task["required"] = False
    elif change == "source":
        task["source_ref"] = "new-source"
    result = execute(tmp_path, d)
    assert result.payload.errors[0].code == "TASK_CHANGE_NOT_SUPPORTED"
    assert state(tmp_path)[1] is None


@pytest.mark.parametrize(
    "case,code",
    [
        ("context", "CONTEXT_CHANGED"),
        ("run", "RUN_NOT_FOUND"),
        ("grid", "RUN_NOT_FOUND"),
        ("revision", "INPUT_REVISION_CONFLICT"),
        ("old_issued", "INPUT_OUT_OF_ORDER"),
        ("future_issued", "INPUT_EXPIRED_OR_FUTURE"),
        ("expired", "INPUT_EXPIRED_OR_FUTURE"),
        ("missing_bus", "IDENTITY_MISMATCH"),
        ("missing_resource", "IDENTITY_MISMATCH"),
        ("duplicate_point", "FORECAST_COVERAGE_INSUFFICIENT"),
        ("tariff_gap", "FORECAST_COVERAGE_INSUFFICIENT"),
        ("short_coverage", "FORECAST_COVERAGE_INSUFFICIENT"),
        ("task_coverage", "INVALID_MESSAGE"),
        ("capacity", "INVALID_MESSAGE"),
    ],
)
def test_invalid_updates_are_rejected_and_recorded(tmp_path, case, code):
    doc, init = initialized(tmp_path)
    d = update_document(doc, init, offset_minutes=15)
    p = d["payload"]
    if case == "context":
        p["context_id"] = "old-context"
    elif case == "run":
        d["run_id"] = "missing"
    elif case == "grid":
        d["microgrid_id"] = "another-grid"
    elif case == "revision":
        p["input_revision"] = "forecast-1"
    elif case == "old_issued":
        p["issued_at"] = (NOW - timedelta(hours=1)).isoformat()
    elif case == "future_issued":
        p["issued_at"] = (NOW + timedelta(minutes=1)).isoformat()
    elif case == "expired":
        p["valid_until"] = NOW.isoformat()
    elif case == "missing_bus":
        p["forecast_points"][0]["bus_loads"].pop()
    elif case == "missing_resource":
        p["forecast_points"][0]["renewables"] = []
    elif case == "duplicate_point":
        p["forecast_points"][1]["at"] = p["forecast_points"][0]["at"]
    elif case == "tariff_gap":
        p["tariff_intervals"][0]["start_at"] = NOW.isoformat()
    elif case in ("short_coverage", "task_coverage"):
        stop = (
            NOW + timedelta(minutes=30 if case == "short_coverage" else 110)
        ).isoformat()
        p["valid_until"] = p["forecast_points"][-1]["at"] = p["tariff_intervals"][-1][
            "end_at"
        ] = stop
    elif case == "capacity":
        p["forecast_points"][0]["renewables"][0]["available_forecast_mw"] = 100.0
    before = state(tmp_path)
    result = execute(tmp_path, d)
    assert result.payload.errors[0].code == code, result
    assert result.payload.data.activation_status == "rejected"
    assert state(tmp_path) == before
    assert execute(tmp_path, d) == result


def test_network_proof_failure_keeps_previous_active_evidence(tmp_path):
    doc, init = initialized(tmp_path)
    d = update_document(doc, init)
    active = execute(tmp_path, d)
    before = state(tmp_path)
    d["message_id"] = "bad-network"
    d["payload"]["input_revision"] = "forecast-3"
    d["payload"]["context_id"] = active.payload.data.new_context_id
    load = d["payload"]["forecast_points"][-1]["bus_loads"][-1]
    load["p_forecast_mw"] = 6.0
    load["p_bounds_mw"] = {"low": 6.0, "high": 6.0}
    result = execute(tmp_path, d)
    assert result.payload.data.activation_status == "rejected", result
    assert state(tmp_path) == before


def test_concurrent_competing_updates_and_idempotent_restarts(tmp_path):
    doc, init = initialized(tmp_path)
    a = update_document(doc, init)
    b = deepcopy(a)
    b["message_id"] = "update-2"
    b["payload"]["input_revision"] = "forecast-3"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda d: execute(tmp_path, d), [a, b]))
    assert sorted(r.payload.data.activation_status for r in results) == [
        "activated",
        "rejected",
    ]
    for d, r in zip([a, b], results):
        assert execute(tmp_path, d) == r
    winner = a if results[0].payload.outcome == "accepted" else b
    changed = deepcopy(winner)
    changed["payload"]["cost_rates"]["loss_cny_per_mwh"] = 50.0
    assert execute(tmp_path, changed).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"


def test_cross_operation_message_ids_and_reserved_staged_revisions(tmp_path):
    doc, init = initialized(tmp_path)
    d = update_document(doc, init, offset_minutes=15)
    d["message_id"] = doc["message_id"]
    assert execute(tmp_path, d).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    d["message_id"] = "update-stage"
    assert execute(tmp_path, d).payload.data.activation_status == "staged"
    doc["message_id"] = "update-stage"
    response = create_field_initializer(tmp_path, clock=lambda: NOW).execute(
        InitializeRequest.model_validate(doc)
    )
    assert response.payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    d["message_id"] = "update-stage-other-id"
    assert execute(tmp_path, d).payload.errors[0].code == "INPUT_REVISION_CONFLICT"


def test_transaction_failure_rolls_back_evidence_response_and_context(tmp_path):
    doc, init = initialized(tmp_path)
    d = update_document(doc, init)
    before = state(tmp_path)
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_head BEFORE INSERT ON planning_heads BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        execute(tmp_path, d)
    assert state(tmp_path) == before
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM planning_updates").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_head")
    assert execute(tmp_path, d).payload.data.activation_status == "activated"


def test_existing_initialization_database_is_migrated_additively(tmp_path):
    doc, init = initialized(tmp_path)
    before = state(tmp_path)[0]
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        db.execute("DROP TABLE planning_heads")
        db.execute("DROP TABLE planning_updates")
    assert (
        execute(tmp_path, update_document(doc, init)).payload.data.activation_status
        == "activated"
    )
    assert state(tmp_path)[0] == before


def test_validation_budget_rejects_even_a_staged_update(tmp_path):
    from oilfield_energy.workflows.field_initialization.api import UpdatePlanningInputs
    from oilfield_energy.workflows.field_initialization.adapters.planning_storage import (
        SqlitePlanningUpdates,
    )

    doc, init = initialized(tmp_path)
    ticks = iter([0.0, 31.0])
    service = UpdatePlanningInputs(
        SqlitePlanningUpdates(tmp_path / "sessions.sqlite3"),
        lambda: NOW,
        lambda: next(ticks),
    )
    response = service.execute(
        UpdatePlanningRequest.model_validate(
            update_document(doc, init, offset_minutes=15)
        )
    )
    assert response.payload.errors[0].code == "COMPUTE_BUDGET_EXCEEDED"
    assert state(tmp_path)[1] is None


def test_boundary_crossing_during_proof_stages_instead_of_promoting_old_state(
    tmp_path, monkeypatch
):
    import oilfield_energy.workflows.field_initialization.application_updates as updates

    original = request_document()
    original["payload"]["control_policy"]["telemetry_max_age_seconds"] = 300.0
    doc, init = initialized(tmp_path, original)
    current_time = [NOW]
    certify = updates.certify_conditions

    def finish_after_boundary(*args, **kwargs):
        result = certify(*args, **kwargs)
        current_time[0] += timedelta(minutes=1)
        return result

    monkeypatch.setattr(updates, "certify_conditions", finish_after_boundary)
    service = create_planning_updater(tmp_path, clock=lambda: current_time[0])
    response = service.execute(
        UpdatePlanningRequest.model_validate(update_document(doc, init))
    )
    assert response.payload.data.activation_status == "staged"
    assert response.payload.data.energy_task_guarantee == "not_assessed"
    assert state(tmp_path)[1] is None


def test_update_does_not_execute_device_cycles(tmp_path, monkeypatch):
    from oilfield_energy.workflows.single_execution.api import UnifiedSingleController

    doc, init = initialized(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Planning updates must not advance device states")

    monkeypatch.setattr(UnifiedSingleController, "advance", forbidden)
    assert (
        execute(tmp_path, update_document(doc, init)).payload.data.activation_status
        == "activated"
    )


@pytest.mark.parametrize("optional", [False, True])
def test_absent_or_unproven_optional_task_is_reported_explicitly(tmp_path, optional):
    doc = request_document()
    if optional:
        doc["payload"]["planning_inputs"]["energy_task"].update(
            required=False,
            deadline_at=(NOW + timedelta(minutes=1)).isoformat(),
            energy_min_mwh=1.8,
            energy_max_mwh=1.9,
        )
    else:
        doc["payload"]["planning_inputs"]["energy_task"] = None
    doc, init = initialized(tmp_path, doc)
    response = execute(tmp_path, update_document(doc, init))
    assert response.payload.data.activation_status == "activated", response
    assert response.payload.data.energy_task_guarantee == (
        "unproven" if optional else "not_required"
    )

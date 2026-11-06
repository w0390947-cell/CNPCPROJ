"""Observation evidence and persistent guards without executing a device cycle."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from oilfield_energy.bootstrap.field_initialization import (
    create_field_initializer,
    create_snapshot_submitter,
    create_pcc_target_requester,
    create_planning_updater,
)
from oilfield_energy.workflows.field_initialization.contracts import (
    InitializeRequest,
    SubmitSnapshotRequest,
    RequestPccTargetRequest,
    UpdatePlanningRequest,
)
from tests.support.field_initialization import NOW
from tests.support.field_planning import initialized, update_document
from tests.support.field_targets import target_case, target_document
from tests.support.field_snapshots import snapshot_document


def submit(root, doc, now=NOW):
    return create_snapshot_submitter(root, clock=lambda: now).execute(
        SubmitSnapshotRequest.model_validate(doc)
    )


def rows(root, table):
    with sqlite3.connect(root / "sessions.sqlite3") as db:
        return db.execute(f"SELECT * FROM {table}").fetchall()


def test_snapshot_maps_ids_and_preserves_state_and_original_evidence(
    tmp_path, monkeypatch
):
    original, init = initialized(tmp_path)
    initial_rows = rows(tmp_path, "initialization_sessions")
    from oilfield_energy.workflows.single_execution.api import UnifiedSingleController

    def forbidden(*args, **kwargs):
        raise AssertionError("Snapshot admission must never simulate execution")

    monkeypatch.setattr(UnifiedSingleController, "advance", forbidden)
    d = snapshot_document(original, init)
    d["payload"]["bus_loads"].reverse()
    d["payload"]["device_states"].reverse()
    response = submit(tmp_path, d)
    result = response.payload.data
    assert response.payload.outcome == "accepted", response
    assert (result.input_status, result.state_status) == ("valid", "consistent")
    assert (
        not result.execution_confirmed
        and not result.dispatch_enabled
        and not result.resync_required
    )
    assert rows(tmp_path, "initialization_sessions") == initial_rows
    assert not rows(tmp_path, "planning_heads") and not rows(
        tmp_path, "pcc_target_heads"
    )
    saved = json.loads(rows(tmp_path, "snapshot_requests")[0][-1])
    assert saved["step"] == 0 and saved["observed_state"]["energy_mwh"] == 1
    assert saved["network"]["certified"]
    # Lossless line: modeled real PCC power equals the 1 MW pure load.
    assert (
        saved["network"]["pcc_p_mw"]["low"] <= 1 <= saved["network"]["pcc_p_mw"]["high"]
    )


def test_actual_minute_forecast_is_used_without_resetting_task_clock(tmp_path):
    document = target_case()
    end_load = deepcopy(
        document["payload"]["planning_inputs"]["forecast_points"][-1]["bus_loads"]
    )
    end_load[1].update(p_forecast_mw=2.0, p_bounds_mw=dict(low=2.0, high=2.0))
    document["payload"]["planning_inputs"]["forecast_points"][-1]["bus_loads"] = (
        end_load
    )
    original, init = initialized(tmp_path, document)
    d = snapshot_document(original, init, minute=60)
    d["payload"]["bus_loads"][1]["p_mw"]["value"] = 1.5
    d["payload"]["pcc"]["p_import_mw"]["value"] = 1.5
    response = submit(tmp_path, d, NOW + timedelta(minutes=60))
    assert response.payload.data.state_status == "consistent", response
    assert response.payload.data.resync_required
    assert response.payload.data.guarantee_status == "unconfirmed"
    saved = json.loads(rows(tmp_path, "snapshot_requests")[0][-1])
    assert saved["step"] == 60
    evidence = json.loads(rows(tmp_path, "initialization_sessions")[0][-1])
    assert evidence["domain"]["energy_task"]["deadline_step"] == 120


@pytest.mark.parametrize(
    "case,code,state",
    [
        ("quality", "TELEMETRY_INVALID", "unconfirmed"),
        ("null", "TELEMETRY_INVALID", "unconfirmed"),
        ("energy", "TELEMETRY_INVALID", "unconfirmed"),
        ("average", "TELEMETRY_INVALID", "unconfirmed"),
        ("estimated", "MODEL_UNSUPPORTED", "outside_model"),
        ("missing_bus", "IDENTITY_MISMATCH", "unconfirmed"),
        ("duplicate", "IDENTITY_MISMATCH", "unconfirmed"),
        ("source", "IDENTITY_MISMATCH", "unconfirmed"),
        ("scope", "METERING_SCOPE_UNCONFIRMED", "unconfirmed"),
        ("remote", "AUTHORITY_LOST", "outside_model"),
        ("root_voltage", "MODEL_UNSUPPORTED", "outside_model"),
        ("model_revision", "CONTEXT_CHANGED", "outside_model"),
        ("operating_state", "CONTEXT_CHANGED", "outside_model"),
        ("load_bound", "OBSERVATION_OUTSIDE_ENVELOPE", "outside_model"),
        ("availability", "OBSERVATION_OUTSIDE_ENVELOPE", "outside_model"),
        ("device", "STATE_OUTSIDE_CERTIFICATE", "outside_model"),
        ("pcc", "STATE_OUTSIDE_CERTIFICATE", "outside_model"),
        ("future_sample", "TELEMETRY_INVALID", "unconfirmed"),
    ],
)
def test_invalid_and_outside_model_facts_are_preserved(tmp_path, case, code, state):
    original, init = initialized(tmp_path)
    d = snapshot_document(original, init)
    s = d["payload"]
    if case == "quality":
        s["bus_loads"][1]["p_mw"]["quality"] = "bad"
    if case == "null":
        s["bus_loads"][1]["p_mw"]["value"] = None
    if case == "energy":
        s["device_states"][1]["energy_mwh"] = None
    if case == "average":
        s["bus_loads"][1]["p_mw"].update(
            value_semantics="interval_average",
            interval_start=(NOW - timedelta(minutes=1)).isoformat(),
            interval_end=NOW.isoformat(),
        )
    if case == "estimated":
        s["bus_loads"][1]["p_mw"]["source_kind"] = "estimated"
    if case == "missing_bus":
        s["bus_loads"].pop()
    if case == "duplicate":
        s["device_states"].append(deepcopy(s["device_states"][0]))
    if case == "source":
        s["bus_loads"][1]["p_mw"]["source_ref"] = "unknown-point"
    if case == "scope":
        s["bus_loads"][1]["scope_ref"] = "net-feeder"
    if case == "remote":
        s["device_states"][0]["remote_enabled"] = False
    if case == "root_voltage":
        s["pcc"]["voltage_pu"]["value"] = 0.99
    if case == "model_revision":
        s["model_revision"] = "model-2"
    if case == "operating_state":
        s["operating_state_ref"] = "other-switch-state"
    if case == "load_bound":
        s["bus_loads"][1]["p_mw"]["value"] = 2.0
    if case == "availability":
        s["renewable_availability"][0]["available_p_mw"]["value"] = 0.1
    if case == "device":
        s["device_states"][1]["energy_mwh"]["value"] = 3.0
    if case == "pcc":
        s["pcc"]["p_import_mw"]["value"] = 4.0
    if case == "future_sample":
        s["device_states"][0]["p_mw"]["observed_at"] = (
            NOW + timedelta(days=1)
        ).isoformat()
    response = submit(tmp_path, d)
    assert response.payload.outcome == "rejected", response
    assert code in [x.code for x in response.payload.errors], response
    assert response.payload.data.state_status == state
    assert response.payload.data.resync_required
    saved = json.loads(rows(tmp_path, "snapshot_requests")[0][6])
    assert SubmitSnapshotRequest.model_validate(
        saved
    ) == SubmitSnapshotRequest.model_validate(d)
    assert rows(tmp_path, "observation_barriers")
    assert not rows(tmp_path, "snapshot_valid_heads")


def test_failure_blocks_old_target_and_planning_activation_and_does_not_self_clear(
    tmp_path,
):
    original, init = initialized(tmp_path, target_case())
    target = target_document(init)
    requester = create_pcc_target_requester(tmp_path, clock=lambda: NOW)
    assert (
        requester.execute(
            RequestPccTargetRequest.model_validate(target)
        ).payload.outcome
        == "accepted"
    )
    head = rows(tmp_path, "pcc_target_heads")
    d = snapshot_document(original, init)
    d["payload"]["bus_loads"][1]["p_mw"]["value"] = 2.0
    assert submit(tmp_path, d).payload.data.guarantee_status == "invalidated"
    target["message_id"] = "new-target-attempt"
    response = requester.execute(RequestPccTargetRequest.model_validate(target))
    assert response.payload.errors[0].code == "RESYNC_REQUIRED"
    assert response.payload.data.previous_strategy_status == "unconfirmed"
    plan = update_document(original, init)
    update = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(plan)
    )
    assert update.payload.data.activation_status == "staged"
    assert "RESYNC_REQUIRED" in update.payload.data.reason
    good = submit(tmp_path, snapshot_document(original, init, sequence=3))
    assert good.payload.data.state_status == "consistent"
    assert (
        good.payload.data.guarantee_status == "invalidated"
        and good.payload.data.resync_required
    )
    assert rows(tmp_path, "pcc_target_heads") == head and not rows(
        tmp_path, "planning_heads"
    )


def test_later_snapshot_does_not_activate_staged_inputs(tmp_path):
    original, init = initialized(tmp_path)
    update = update_document(original, init, offset_minutes=15)
    response = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(update)
    )
    assert response.payload.data.activation_status == "staged"
    d = snapshot_document(original, init, minute=15)
    assert (
        submit(tmp_path, d, NOW + timedelta(minutes=15)).payload.outcome == "accepted"
    )
    assert not rows(tmp_path, "planning_heads")


@pytest.mark.parametrize(
    "case", ["sequence", "boundary", "sample", "context", "future", "expired_message"]
)
def test_late_or_unrelated_messages_do_not_replace_newer_observations(tmp_path, case):
    original, init = initialized(tmp_path)
    good = snapshot_document(original, init, minute=1, sequence=10)
    now = NOW + timedelta(minutes=1)
    submit(tmp_path, good, now)
    head, barrier = (
        rows(tmp_path, "snapshot_heads"),
        rows(tmp_path, "observation_barriers"),
    )
    d = snapshot_document(original, init, minute=1, sequence=11)
    if case == "sequence":
        d["payload"]["sequence"] = 9
    if case == "boundary":
        d["payload"]["snapshot_at"] = NOW.isoformat()
    if case == "sample":
        d["payload"]["device_states"][0]["p_mw"]["observed_at"] = NOW.isoformat()
    if case == "context":
        d["payload"]["context_id"] = "obsolete-context"
    if case == "future":
        d["payload"]["received_at"] = (now + timedelta(seconds=1)).isoformat()
    if case == "expired_message":
        d["created_at"] = NOW.isoformat()
    result = submit(tmp_path, d, now)
    assert result.payload.outcome == "rejected"
    assert result.payload.data.observation_status == "archived"
    assert rows(tmp_path, "snapshot_heads") == head
    assert rows(tmp_path, "observation_barriers") == barrier


def test_expired_coverage_is_not_extended_with_last_forecast(tmp_path):
    original, init = initialized(tmp_path)
    # Keep authority alive past forecast expiry, so coverage guard is tested directly.
    original["payload"]["authority"]["valid_until"] = (
        NOW + timedelta(hours=3)
    ).isoformat()
    other = tmp_path / "coverage"
    original, init = initialized(other, original)
    result = submit(
        other,
        snapshot_document(original, init, minute=120),
        NOW + timedelta(minutes=120),
    )
    assert result.payload.errors[0].code == "FORECAST_COVERAGE_INSUFFICIENT"
    assert result.payload.data.state_status == "unconfirmed"


def test_replay_restart_concurrency_and_immutable_snapshot_ids(tmp_path):
    original, init = initialized(tmp_path)
    d = snapshot_document(original, init)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(tmp_path, d), range(4)))
    assert all(x == results[0] for x in results)
    assert len(rows(tmp_path, "snapshot_requests")) == 1
    assert submit(tmp_path, d, NOW + timedelta(days=1)) == results[0]
    changed = deepcopy(d)
    changed["payload"]["device_states"][1]["energy_mwh"]["value"] = 1.1
    assert submit(tmp_path, changed).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    changed["message_id"] = "other-message"
    assert submit(tmp_path, changed).payload.errors[0].code == "SNAPSHOT_ID_CONFLICT"
    repeat = deepcopy(d)
    repeat["message_id"] = "same-snapshot-other-envelope"
    again = submit(tmp_path, repeat, NOW + timedelta(days=1))
    assert again.payload == results[0].payload
    assert again.correlation_id == repeat["message_id"]
    assert len(rows(tmp_path, "snapshot_heads")) == 1
    reserved = snapshot_document(original, init, sequence=3)
    reserved["payload"]["snapshot_id"] = "snapshot-1"
    assert submit(tmp_path, reserved).payload.errors[0].code == "SNAPSHOT_ID_CONFLICT"


def test_transaction_failure_rolls_back_raw_snapshot_heads_and_barrier(tmp_path):
    original, init = initialized(tmp_path)
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER reject_barrier BEFORE INSERT ON observation_barriers BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    d = snapshot_document(original, init)
    d["payload"]["device_states"][0]["blocked"] = True
    with pytest.raises(sqlite3.IntegrityError):
        submit(tmp_path, d)
    assert not rows(tmp_path, "snapshot_requests")
    assert not rows(tmp_path, "snapshot_heads")
    assert not rows(tmp_path, "observation_barriers")


def test_message_namespace_shared_by_all_four_operations(tmp_path):
    original, init = initialized(tmp_path, target_case())
    d = snapshot_document(original, init)
    submit(tmp_path, d)
    original["message_id"] = d["message_id"]
    response = create_field_initializer(tmp_path, clock=lambda: NOW).execute(
        InitializeRequest.model_validate(original)
    )
    assert response.payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    for factory, cls, doc in (
        (
            create_planning_updater,
            UpdatePlanningRequest,
            update_document(original, init),
        ),
        (create_pcc_target_requester, RequestPccTargetRequest, target_document(init)),
    ):
        doc["message_id"] = d["message_id"]
        result = factory(tmp_path, clock=lambda: NOW).execute(cls.model_validate(doc))
        assert result.payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    d["message_id"] = "message-1"
    assert submit(tmp_path, d).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"


@pytest.mark.parametrize("value", ["1.0", True, float("nan"), float("inf")])
def test_invalid_wire_numbers_rejected_without_any_state_write(tmp_path, value):
    original, init = initialized(tmp_path)
    d = snapshot_document(original, init)
    d["payload"]["pcc"]["p_import_mw"]["value"] = value
    with pytest.raises(ValidationError):
        submit(tmp_path, d)
    assert not rows(tmp_path, "snapshot_requests")


def test_unknown_run_and_wrong_grid_do_not_affect_other_session(tmp_path):
    original, init = initialized(tmp_path)
    for key in ("run_id", "microgrid_id"):
        d = snapshot_document(original, init)
        d[key] = "unknown"
        d["message_id"] = "unknown-" + key
        result = submit(tmp_path, d)
        assert result.payload.errors[0].code == "RUN_NOT_FOUND"
    assert not rows(tmp_path, "snapshot_heads") and not rows(
        tmp_path, "observation_barriers"
    )


def test_budget_expiry_records_unconfirmed_instead_of_success(tmp_path):
    from oilfield_energy.workflows.field_initialization.api import SubmitSnapshot
    from oilfield_energy.workflows.field_initialization.adapters.snapshot_storage import (
        SqliteSnapshots,
    )

    original, init = initialized(tmp_path)
    elapsed = iter((0.0, 31.0))
    service = SubmitSnapshot(
        SqliteSnapshots(tmp_path / "sessions.sqlite3"),
        lambda: NOW,
        lambda: next(elapsed),
    )
    result = service.execute(
        SubmitSnapshotRequest.model_validate(snapshot_document(original, init))
    )
    assert result.payload.errors[0].code == "COMPUTE_BUDGET_EXCEEDED"
    assert result.payload.data.guarantee_status == "unconfirmed"
    assert not rows(tmp_path, "snapshot_valid_heads")


def test_concurrent_snapshot_failure_and_target_request_cannot_leave_unguarded_head(
    tmp_path,
):
    original, init = initialized(tmp_path, target_case())
    d = snapshot_document(original, init)
    d["payload"]["pcc"]["p_import_mw"]["value"] = 3.0
    target = RequestPccTargetRequest.model_validate(target_document(init))
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot_job = pool.submit(submit, tmp_path, d)
        target_job = pool.submit(
            create_pcc_target_requester(tmp_path, clock=lambda: NOW).execute, target
        )
        assert snapshot_job.result().payload.data.guarantee_status == "invalidated"
        assert target_job.result().payload.outcome in ("accepted", "rejected")
    # Regardless of order, all subsequent admissions see the durable guard.
    target = target.model_copy(update={"message_id": "after-race"})
    result = create_pcc_target_requester(tmp_path, clock=lambda: NOW).execute(target)
    assert result.payload.errors[0].code == "RESYNC_REQUIRED"


def test_existing_sessions_migrate_additively_and_keep_historical_evidence(tmp_path):
    original, init = initialized(tmp_path)
    history = rows(tmp_path, "initialization_sessions")
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        for table in (
            "snapshot_requests",
            "snapshot_heads",
            "snapshot_valid_heads",
            "observation_barriers",
        ):
            db.execute(f"DROP TABLE {table}")
    assert (
        submit(tmp_path, snapshot_document(original, init)).payload.outcome
        == "accepted"
    )
    assert rows(tmp_path, "initialization_sessions") == history


def test_large_finite_measurement_is_reported_without_clipping_or_crashing(tmp_path):
    original, init = initialized(tmp_path)
    d = snapshot_document(original, init)
    d["payload"]["device_states"][1]["p_mw"]["value"] = 1e300
    response = submit(tmp_path, d)
    assert response.payload.outcome == "rejected"
    assert response.payload.data.state_status == "outside_model"
    saved = json.loads(rows(tmp_path, "snapshot_requests")[0][6])
    assert saved["payload"]["device_states"][1]["p_mw"]["value"] == 1e300


def test_target_deadline_and_tube_assessed_from_observation_without_execution_claim(
    tmp_path,
):
    original, init = initialized(tmp_path, target_case())
    target = target_document(init)
    target["payload"]["target_p_import_mw"] = 1.1
    accepted = create_pcc_target_requester(tmp_path, clock=lambda: NOW).execute(
        RequestPccTargetRequest.model_validate(target)
    )
    assert accepted.payload.outcome == "accepted", accepted
    d = snapshot_document(original, init, minute=2)
    result = submit(tmp_path, d, NOW + timedelta(minutes=2))
    assert "TARGET_TRACKING_VIOLATED" in {e.code for e in result.payload.errors}
    assert result.payload.data.guarantee_status == "invalidated"
    assert not result.payload.data.execution_confirmed


def test_stale_during_assessment_is_recorded_but_not_marked_valid(tmp_path):
    from oilfield_energy.workflows.field_initialization.api import SubmitSnapshot
    from oilfield_energy.workflows.field_initialization.adapters.snapshot_storage import (
        SqliteSnapshots,
    )

    original, init = initialized(tmp_path)
    ticks = iter((NOW, NOW + timedelta(seconds=31)))

    def clock():
        return next(ticks, NOW + timedelta(seconds=31))

    service = SubmitSnapshot(SqliteSnapshots(tmp_path / "sessions.sqlite3"), clock)
    result = service.execute(
        SubmitSnapshotRequest.model_validate(snapshot_document(original, init))
    )
    assert result.payload.errors[0].code == "TELEMETRY_INVALID"
    assert result.payload.data.state_status == "unconfirmed"
    assert not rows(tmp_path, "snapshot_valid_heads")


def test_snapshot_identity_normalizes_timezone_and_id_order(tmp_path):
    original, init = initialized(tmp_path)
    d = snapshot_document(original, init)
    first = submit(tmp_path, d)
    same = SubmitSnapshotRequest.model_validate(d).model_dump()
    same["message_id"] = "same-facts-another-envelope"

    def local(value):
        if isinstance(value, dict):
            return {k: local(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [local(x) for x in value]
        if isinstance(value, datetime):
            return value.astimezone(timezone(timedelta(hours=8))).isoformat()
        return value

    same = local(same)
    same["payload"]["bus_loads"].reverse()
    same["payload"]["device_states"].reverse()
    replay = submit(tmp_path, same)
    assert replay.payload == first.payload
    assert len(rows(tmp_path, "snapshot_heads")) == 1


def test_missing_points_do_not_erase_previous_sample_time_watermarks(tmp_path):
    original, init = initialized(tmp_path)
    now = NOW + timedelta(seconds=1)
    first = snapshot_document(original, init)
    first["created_at"] = now.isoformat()
    first["payload"]["received_at"] = now.isoformat()
    first["payload"]["bus_loads"][1]["p_mw"]["observed_at"] = (
        NOW + timedelta(seconds=0.5)
    ).isoformat()
    assert submit(tmp_path, first, now).payload.outcome == "accepted"
    missing = snapshot_document(original, init, sequence=3)
    missing["created_at"] = now.isoformat()
    missing["payload"]["received_at"] = now.isoformat()
    missing["payload"]["bus_loads"].pop()
    assert submit(tmp_path, missing, now).payload.errors[0].code == "IDENTITY_MISMATCH"
    older = snapshot_document(original, init, sequence=4)
    older["created_at"] = now.isoformat()
    older["payload"]["received_at"] = now.isoformat()
    older["payload"]["bus_loads"][1]["p_mw"]["observed_at"] = (
        NOW + timedelta(seconds=0.25)
    ).isoformat()
    result = submit(tmp_path, older, now)
    assert result.payload.errors[0].code == "SNAPSHOT_OUT_OF_ORDER"
    assert result.payload.data.observation_status == "archived"


def test_invalid_future_sample_does_not_prevent_later_valid_observation(tmp_path):
    original, init = initialized(tmp_path)
    bad = snapshot_document(original, init)
    bad["payload"]["bus_loads"][1]["p_mw"]["observed_at"] = (
        NOW + timedelta(days=1)
    ).isoformat()
    assert submit(tmp_path, bad).payload.errors[0].code == "TELEMETRY_INVALID"
    good = submit(tmp_path, snapshot_document(original, init, sequence=3))
    assert good.payload.data.state_status == "consistent"
    assert good.payload.data.resync_required

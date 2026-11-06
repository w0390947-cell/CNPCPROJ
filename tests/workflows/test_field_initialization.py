"""Initialization proves feasibility, persists evidence, and never advances a plant."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256

import pytest
from pydantic import ValidationError

from oilfield_energy.bootstrap.field_initialization import create_field_initializer
from oilfield_energy.workflows.field_initialization.contracts import (
    InitializeRequest,
    InitializationEvidence,
)
from tests.support.field_initialization import NOW, request_document


def parse(value):
    return InitializeRequest.model_validate_json(json.dumps(value))


def test_initializes_using_real_network_and_task_certificates(tmp_path):
    request = parse(request_document())
    response = create_field_initializer(tmp_path, clock=lambda: NOW).execute(request)
    assert response.payload.outcome == "accepted", response
    result = response.payload.data
    assert result.energy_task_guarantee == "maintained" and not result.dispatch_enabled
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        saved = db.execute("SELECT evidence FROM initialization_sessions").fetchone()[0]
    evidence = InitializationEvidence.model_validate_json(saved)
    assert evidence.initial.p_mw == (0, 0)
    assert evidence.initial.energy_mwh == 1
    assert evidence.domain.forecasts[0].generation[0].minimum_available_mw == 0.45
    assert evidence.plan.task_continuation is not None
    assert evidence.terminal_proof_digest
    assert evidence.request == request


def test_replay_survives_restart_and_concurrent_requests(tmp_path):
    request = parse(request_document())

    def execute(_):
        return create_field_initializer(tmp_path, clock=lambda: NOW).execute(request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(execute, range(4)))
    assert all(x == results[0] for x in results)
    assert results[0].payload.outcome == "accepted"
    changed = request_document()
    changed["payload"]["planning_inputs"]["cost_rates"]["loss_cny_per_mwh"] = 20.0
    conflict = create_field_initializer(tmp_path, clock=lambda: NOW).execute(
        parse(changed)
    )
    assert conflict.payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    changed["message_id"] = "message-2"
    assert (
        create_field_initializer(tmp_path, clock=lambda: NOW)
        .execute(parse(changed))
        .payload.errors[0]
        .code
        == "ACTIVE_RUN_EXISTS"
    )


@pytest.mark.parametrize(
    "case,code",
    [
        ("closed_loop", "CLOSED_LOOP_UNAVAILABLE"),
        ("stale", "TELEMETRY_INVALID"),
        ("mapping", "IDENTITY_MISMATCH"),
        ("duplicate", "IDENTITY_MISMATCH"),
        ("scope", "IDENTITY_MISMATCH"),
        ("availability", "OBSERVATION_OUTSIDE_ENVELOPE"),
        ("task", "TASK_UNPROVEN"),
        ("forecast", "FORECAST_COVERAGE_INSUFFICIENT"),
        ("voltage", "MODEL_UNSUPPORTED"),
        ("synthetic", "MODEL_UNSUPPORTED"),
        ("unknown_network", "INVALID_MESSAGE"),
        ("pcc", "STATE_OUTSIDE_CERTIFICATE"),
    ],
)
def test_rejects_invalid_or_unproven_initialization_without_session(
    tmp_path, case, code
):
    d = request_document()
    p = d["payload"]
    if case == "closed_loop":
        p["mode"] = "closed_loop"
    if case == "stale":
        p["initial_snapshot"]["device_states"][0]["p_mw"]["observed_at"] = (
            "2026-10-15T01:00:00Z"
        )
    if case == "mapping":
        p["initial_snapshot"]["device_states"][0]["p_mw"]["source_ref"] = "wrong"
    if case == "duplicate":
        p["initial_snapshot"]["device_states"].append(
            deepcopy(p["initial_snapshot"]["device_states"][0])
        )
    if case == "scope":
        p["model_package"]["load_scopes"][1]["members"] = ["pcc"]
    if case == "availability":
        p["initial_snapshot"]["renewable_availability"][0]["available_p_mw"][
            "value"
        ] = 0.1
    if case == "task":
        p["planning_inputs"]["energy_task"].update(
            deadline_at="2026-10-15T02:01:00Z", energy_min_mwh=1.8, energy_max_mwh=1.9
        )
    if case == "forecast":
        p["planning_inputs"]["forecast_points"].reverse()
    if case == "voltage":
        p["initial_snapshot"]["pcc"]["voltage_pu"]["value"] = 0.9
    if case == "synthetic":
        p["model_package"]["network_model"]["provenance"].update(
            synthetic=True, approved_for_field_use=False
        )
    if case == "unknown_network":
        p["model_package"]["network_model"]["branches"][0]["unknown"] = 1
    if case == "pcc":
        p["initial_snapshot"]["pcc"]["p_import_mw"]["value"] = 4.0
    response = create_field_initializer(tmp_path, clock=lambda: NOW).execute(parse(d))
    assert response.payload.outcome == "rejected"
    assert response.payload.errors[0].code == code, response
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert (
            db.execute("SELECT count(*) FROM initialization_sessions").fetchone()[0]
            == 0
        )


def test_content_addressed_network_and_mapping(tmp_path):
    d = request_document()
    package = d["payload"]["model_package"]
    objects = tmp_path / "objects"
    objects.mkdir()
    for key in ["network_model", "point_mapping"]:
        raw = json.dumps(package[key]).encode()
        digest = sha256(raw).hexdigest()
        (objects / f"{digest}.json").write_bytes(raw)
        package[key] = dict(artifact_id=key, revision="1", sha256=digest)
    result = create_field_initializer(tmp_path, clock=lambda: NOW).execute(parse(d))
    assert result.payload.outcome == "accepted", result


@pytest.mark.parametrize("bad", ["1.0", True, float("nan")])
def test_numeric_contract_does_not_coerce_invalid_power(bad):
    d = request_document()
    d["payload"]["initial_snapshot"]["device_states"][0]["p_mw"]["value"] = bad
    with pytest.raises(ValidationError):
        parse(d)


def test_initial_energy_is_checked_even_without_an_energy_task(tmp_path):
    d = request_document()
    d["payload"]["planning_inputs"]["energy_task"] = None
    storage = next(
        x
        for x in d["payload"]["initial_snapshot"]["device_states"]
        if x.get("energy_mwh") is not None
    )
    storage["energy_mwh"]["value"] = 100.0
    result = create_field_initializer(tmp_path, clock=lambda: NOW).execute(parse(d))
    assert result.payload.outcome == "rejected"
    assert result.payload.errors[0].code == "INITIAL_SAFETY_UNPROVEN"
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert (
            db.execute("SELECT count(*) FROM initialization_sessions").fetchone()[0]
            == 0
        )


def test_optional_task_is_reported_unproven_not_silently_dropped(tmp_path):
    d = request_document()
    d["payload"]["planning_inputs"]["energy_task"].update(
        deadline_at="2026-10-15T02:01:00Z",
        energy_min_mwh=1.8,
        energy_max_mwh=1.9,
        required=False,
    )
    result = create_field_initializer(tmp_path, clock=lambda: NOW).execute(parse(d))
    assert result.payload.outcome == "accepted", result
    assert result.payload.data.energy_task_guarantee == "unproven"


def test_resource_order_is_mapped_by_id_and_initial_state_is_not_simulated(tmp_path):
    d = request_document()
    p = d["payload"]
    p["model_package"]["devices"].reverse()
    p["initial_snapshot"]["device_states"][0]["p_mw"]["value"] = 0.1
    p["initial_snapshot"]["pcc"]["p_import_mw"]["value"] = 0.9
    result = create_field_initializer(tmp_path, clock=lambda: NOW).execute(parse(d))
    assert result.payload.outcome == "accepted", result
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        evidence = InitializationEvidence.model_validate_json(
            db.execute("SELECT evidence FROM initialization_sessions").fetchone()[0]
        )
    assert evidence.initial.p_mw == (0.0, 0.1)
    assert evidence.initial.energy_mwh == 1.0
    assert (
        evidence.plan.commands
    )  # Real safe continuation was constructed, not executed.


def test_tampered_reference_is_rejected(tmp_path):
    d = request_document()
    package = d["payload"]["model_package"]
    raw = json.dumps(package["network_model"]).encode()
    digest = sha256(raw).hexdigest()
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / f"{digest}.json").write_bytes(b"{}")
    package["network_model"] = dict(artifact_id="network", revision="1", sha256=digest)
    result = create_field_initializer(tmp_path, clock=lambda: NOW).execute(parse(d))
    assert result.payload.errors[0].code == "ARTIFACT_INVALID"


def test_actual_ac_violation_cannot_be_returned_ready(tmp_path):
    d = request_document()
    d["payload"]["control_policy"]["pcc_max_mw"] = 0.5
    result = create_field_initializer(tmp_path, clock=lambda: NOW).execute(parse(d))
    assert result.payload.outcome == "rejected"
    assert result.payload.errors[0].code == "INITIAL_SAFETY_UNPROVEN"


def test_initialization_does_not_call_the_physical_executor(tmp_path, monkeypatch):
    from oilfield_energy.workflows.single_execution import preparation

    def forbidden(*args, **kwargs):
        raise AssertionError("Initialization must not execute or commit a device step")

    monkeypatch.setattr(preparation, "execute_tube_command", forbidden)
    result = create_field_initializer(tmp_path, clock=lambda: NOW).execute(
        parse(request_document())
    )
    assert result.payload.outcome == "accepted", result


def test_admission_budget_prevents_late_ready_commit(tmp_path):
    from oilfield_energy.bootstrap.adapters.field_initialization_network import (
        FieldNetworkAssembler,
    )
    from oilfield_energy.workflows.field_initialization.adapters.storage import (
        LocalArtifacts,
        SqliteSessions,
    )
    from oilfield_energy.workflows.field_initialization.api import InitializeRun

    elapsed = iter([0.0, 31.0])
    service = InitializeRun(
        SqliteSessions(tmp_path / "sessions.sqlite3"),
        FieldNetworkAssembler(),
        LocalArtifacts(tmp_path / "objects"),
        lambda: NOW,
        lambda: next(elapsed),
    )
    result = service.execute(parse(request_document()))
    assert result.payload.errors[0].code == "COMPUTE_BUDGET_EXCEEDED"

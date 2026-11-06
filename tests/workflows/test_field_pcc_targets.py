"""Fixed target proofs and durable field decisions; no device simulation."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest

from oilfield_energy.bootstrap.field_initialization import create_pcc_target_requester
from oilfield_energy.bootstrap.field_initialization import (
    create_field_initializer,
    create_planning_updater,
)
from oilfield_energy.workflows.field_initialization.contracts import (
    PccTargetEvidence,
    RequestPccTargetRequest,
)
from tests.support.field_initialization import NOW
from tests.support.field_planning import initialized, update_document
from oilfield_energy.workflows.field_initialization.contracts import (
    InitializeRequest,
    UpdatePlanningRequest,
)
from tests.support.field_targets import target_case, target_document


def execute(root, document, now=NOW):
    return create_pcc_target_requester(root, clock=lambda: now).execute(
        RequestPccTargetRequest.model_validate(document)
    )


def head(root):
    with sqlite3.connect(root / "sessions.sqlite3") as db:
        row = db.execute("SELECT evidence FROM pcc_target_heads").fetchone()
        return PccTargetEvidence.model_validate_json(row[0]) if row else None


def test_accepts_exact_target_with_explicit_deadline_and_preserved_state(tmp_path):
    original, initial = initialized(tmp_path, target_case())
    d = target_document(initial)
    result = execute(tmp_path, d)
    data = result.payload.data
    assert data.target_status == "accepted", result
    assert data.accepted_target == data.requested_target
    assert data.guaranteed_until == NOW + timedelta(minutes=5)
    assert data.response_deadline_at == NOW + timedelta(minutes=2)
    assert data.energy_task_guarantee == "maintained"
    assert not data.dispatch_enabled and data.mode == "shadow"
    evidence = head(tmp_path)
    assert evidence.plan.tracking.deadline_step == 2
    assert evidence.plan.tracking.target_p_mw == 1.0
    assert (
        evidence.plan.initial.energy_mwh.low
        == evidence.plan.initial.energy_mwh.high
        == 1.0
    )
    assert evidence.plan.task_continuation is not None
    assert execute(tmp_path, d, NOW + timedelta(days=1)) == result


@pytest.mark.parametrize(
    "case,code",
    [
        ("run", "RUN_NOT_FOUND"),
        ("grid", "RUN_NOT_FOUND"),
        ("context", "CONTEXT_CHANGED"),
        ("future", "CONFIRMED_STATE_REQUIRED"),
        ("old_state", "CONFIRMED_STATE_REQUIRED"),
        ("stale", "TELEMETRY_INVALID"),
        ("future_message", "TELEMETRY_INVALID"),
        ("unaligned", "TARGET_TIME_UNALIGNED"),
        ("deadline", "TARGET_TIME_INVALID"),
        ("no_hold", "TARGET_TIME_INVALID"),
        ("loose_p", "TARGET_TOLERANCE_UNSUPPORTED"),
        ("loose_q", "TARGET_TOLERANCE_UNSUPPORTED"),
        ("p_limit", "TARGET_OUTSIDE_LIMITS"),
        ("q_limit", "TARGET_OUTSIDE_LIMITS"),
        ("unproven", "TARGET_UNPROVEN"),
        ("coverage", "TARGET_COVERAGE_INSUFFICIENT"),
    ],
)
def test_rejects_invalid_or_unproven_requests_without_a_target_head(
    tmp_path, case, code
):
    _, initial = initialized(tmp_path, target_case())
    d = target_document(initial)
    p, now = d["payload"], NOW
    if case == "run":
        d["run_id"] = "missing"
    elif case == "grid":
        d["microgrid_id"] = "another-grid"
    elif case == "context":
        p["context_id"] = "old-context"
    elif case == "future":
        for key in ("effective_from", "response_deadline_at", "hold_until"):
            from datetime import datetime

            p[key] = (
                datetime.fromisoformat(p[key]) + timedelta(minutes=15)
            ).isoformat()
    elif case in ("old_state", "stale"):
        now += timedelta(seconds=60 if case == "old_state" else 31)
        d["created_at"] = now.isoformat()
    elif case == "future_message":
        d["created_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif case == "unaligned":
        p["response_deadline_at"] = (NOW + timedelta(seconds=121)).isoformat()
    elif case == "deadline":
        p["response_deadline_at"] = p["effective_from"]
    elif case == "no_hold":
        p["hold_until"] = p["response_deadline_at"]
    elif case == "loose_p":
        p["p_tolerance_mw"] = 0.1
    elif case == "loose_q":
        p["q_tolerance_mvar"] = 0.1
    elif case == "p_limit":
        p["target_p_import_mw"] = 6.0
    elif case == "q_limit":
        p["target_q_import_mvar"] = 1e300
    elif case == "unproven":
        p["target_p_import_mw"] = 4.0
    elif case == "coverage":
        p["response_deadline_at"] = (NOW + timedelta(hours=2)).isoformat()
        p["hold_until"] = (NOW + timedelta(hours=3)).isoformat()
    result = execute(tmp_path, d, now)
    assert result.payload.data.target_status == "rejected", result
    assert result.payload.errors[0].code == code, result
    assert result.payload.data.accepted_target is None
    assert head(tmp_path) is None
    assert execute(tmp_path, d, now) == result


def test_partial_guarantee_is_explicit_and_excludes_subminute_authority_tail(tmp_path):
    doc = target_case()
    doc["payload"]["authority"]["valid_until"] = (
        NOW + timedelta(minutes=5, seconds=30)
    ).isoformat()
    _, init = initialized(tmp_path, doc)
    d = target_document(init)
    d["payload"]["hold_until"] = (NOW + timedelta(minutes=10)).isoformat()
    response = execute(tmp_path, d)
    assert response.payload.data.target_status == "accepted", response
    assert response.payload.data.guaranteed_until == NOW + timedelta(minutes=5)
    assert head(tmp_path).request.payload.hold_until == NOW + timedelta(minutes=10)


def test_rejection_preserves_old_target_and_reports_evidence_scope(tmp_path):
    _, init = initialized(tmp_path, target_case())
    d = target_document(init)
    assert execute(tmp_path, d).payload.outcome == "accepted"
    before = head(tmp_path)
    d["message_id"], d["payload"]["target_id"] = "bad-target", "unreachable"
    d["payload"]["target_p_import_mw"] = 4.0
    result = execute(tmp_path, d)
    assert result.payload.data.target_status == "rejected"
    assert result.payload.data.previous_target_id == "external-1"
    assert result.payload.data.previous_strategy_status == "conditional"
    assert head(tmp_path) == before
    d["message_id"] = "late-request"
    d["created_at"] = (NOW + timedelta(minutes=1)).isoformat()
    result = execute(tmp_path, d, NOW + timedelta(minutes=1))
    assert result.payload.data.previous_strategy_status == "unconfirmed"


def test_target_identity_and_current_value_deadline_cannot_be_rewritten(tmp_path):
    _, init = initialized(tmp_path, target_case())
    d = target_document(init)
    assert execute(tmp_path, d).payload.outcome == "accepted"
    before = head(tmp_path)
    other = deepcopy(d)
    other["message_id"] = "different-message"
    other["payload"]["hold_until"] = (NOW + timedelta(minutes=6)).isoformat()
    assert execute(tmp_path, other).payload.errors[0].code == "TARGET_ID_CONFLICT"
    other["payload"]["target_id"] = "new-id-same-values"
    other["message_id"] = "attempt-reset-deadline"
    other["payload"]["response_deadline_at"] = (NOW + timedelta(minutes=3)).isoformat()
    assert execute(tmp_path, other).payload.errors[0].code == "TARGET_DEADLINE_CHANGED"
    assert head(tmp_path) == before
    # A different physical goal is admitted separately; it may replace the old one only after proof.
    other["message_id"] = "replacement"
    other["payload"]["target_id"] = "new-physical-goal"
    other["payload"]["target_p_import_mw"] = 1.1
    assert execute(tmp_path, other).payload.outcome == "accepted"
    assert head(tmp_path).request.payload.target_id == "new-physical-goal"


def test_planning_activation_revalidates_target_in_the_new_context(tmp_path):
    original, init = initialized(tmp_path, target_case())
    target = target_document(init)
    first = execute(tmp_path, target)
    before = head(tmp_path)
    update = update_document(original, init)
    response = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(update)
    )
    assert response.payload.data.activation_status == "activated", response
    assert (
        response.payload.data.preserved_target_deadline_at
        == first.payload.data.response_deadline_at
    )
    renewed = head(tmp_path)
    assert (
        renewed.context_id == response.payload.data.new_context_id != before.context_id
    )
    assert renewed.request == before.request
    assert renewed.guaranteed_until >= before.guaranteed_until
    assert renewed.plan.tracking.deadline_step == before.plan.tracking.deadline_step
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert (
            db.execute("SELECT count(*) FROM pcc_target_revalidations").fetchone()[0]
            == 1
        )
    assert execute(tmp_path, target) == first


def test_unproven_retained_target_blocks_planning_context_change(tmp_path):
    original, init = initialized(tmp_path, target_case())
    assert execute(tmp_path, target_document(init)).payload.outcome == "accepted"
    before = head(tmp_path)
    update = update_document(original, init)
    middle = deepcopy(update["payload"]["forecast_points"][0])
    middle["at"] = (NOW + timedelta(minutes=2)).isoformat()
    middle["bus_loads"][-1].update(
        p_forecast_mw=3.0, p_bounds_mw={"low": 3.0, "high": 3.0}
    )
    update["payload"]["forecast_points"].insert(1, middle)
    response = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(update)
    )
    assert response.payload.errors[0].code == "RETAINED_TARGET_UNPROVEN", response
    assert head(tmp_path) == before
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM planning_heads").fetchone()[0] == 0


def test_message_namespace_and_concurrent_replay_are_shared_across_operations(tmp_path):
    original, init = initialized(tmp_path, target_case())
    d = target_document(init)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: execute(tmp_path, d), range(3)))
    assert all(x == responses[0] for x in responses)
    changed = deepcopy(d)
    changed["payload"]["target_q_import_mvar"] = 0.01
    assert execute(tmp_path, changed).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    original["message_id"] = d["message_id"]
    response = create_field_initializer(tmp_path, clock=lambda: NOW).execute(
        InitializeRequest.model_validate(original)
    )
    assert response.payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    update = update_document(original, init)
    update["message_id"] = d["message_id"]
    response = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(update)
    )
    assert response.payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    d["message_id"] = "message-1"
    assert execute(tmp_path, d).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"


def test_target_transaction_failure_rolls_back_head_and_response(tmp_path):
    _, init = initialized(tmp_path, target_case())
    d = target_document(init)
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_target BEFORE INSERT ON pcc_target_heads BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        execute(tmp_path, d)
    assert head(tmp_path) is None
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM pcc_target_requests").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_target")
    assert execute(tmp_path, d).payload.outcome == "accepted"


def test_stricter_requested_bands_are_used_by_the_actual_proof(tmp_path):
    _, init = initialized(tmp_path, target_case())
    d = target_document(init)
    d["payload"].update(p_tolerance_mw=0.02, q_tolerance_mvar=0.02)
    assert execute(tmp_path, d).payload.outcome == "accepted"
    proof = head(tmp_path)
    assert proof.domain.p_band_mw == proof.domain.q_band_mvar == 0.02
    for p, q in zip(
        proof.plan.tracking.pcc_p_mw[1:], proof.plan.tracking.pcc_q_mvar[1:]
    ):
        assert max(abs(p.low - 1), abs(p.high - 1)) <= 0.02
        assert max(abs(q.low), abs(q.high)) <= 0.02


def test_budget_and_boundary_checks_prevent_a_late_acceptance(tmp_path, monkeypatch):
    import oilfield_energy.workflows.field_initialization.application_targets as application
    from oilfield_energy.workflows.field_initialization.adapters.target_storage import (
        SqlitePccTargets,
    )
    from oilfield_energy.workflows.field_initialization.api import RequestPccTarget

    _, init = initialized(tmp_path, target_case())
    d = target_document(init)
    ticks = iter([0.0, 31.0])
    service = RequestPccTarget(
        SqlitePccTargets(tmp_path / "sessions.sqlite3"),
        lambda: NOW,
        lambda: next(ticks),
    )
    response = service.execute(RequestPccTargetRequest.model_validate(d))
    assert response.payload.errors[0].code == "COMPUTE_BUDGET_EXCEEDED"
    assert head(tmp_path) is None
    now = [NOW]
    certify = application.certify_external_target

    def crosses_boundary(*args, **kwargs):
        result = certify(*args, **kwargs)
        now[0] += timedelta(minutes=1)
        return result

    monkeypatch.setattr(application, "certify_external_target", crosses_boundary)
    d["message_id"] = "late-proof"
    response = create_pcc_target_requester(tmp_path, clock=lambda: now[0]).execute(
        RequestPccTargetRequest.model_validate(d)
    )
    assert response.payload.errors[0].code == "CONFIRMED_STATE_REQUIRED"
    assert head(tmp_path) is None


def test_planning_and_target_evidence_roll_back_together(tmp_path):
    original, init = initialized(tmp_path, target_case())
    assert execute(tmp_path, target_document(init)).payload.outcome == "accepted"
    before = head(tmp_path)
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_renewal BEFORE UPDATE ON pcc_target_heads BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    update = UpdatePlanningRequest.model_validate(update_document(original, init))
    with pytest.raises(sqlite3.IntegrityError):
        create_planning_updater(tmp_path, clock=lambda: NOW).execute(update)
    assert head(tmp_path) == before
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM planning_updates").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM planning_heads").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM pcc_target_revalidations").fetchone()[0]
            == 0
        )


def test_future_planning_is_staged_without_changing_the_target(tmp_path):
    original, init = initialized(tmp_path, target_case())
    assert execute(tmp_path, target_document(init)).payload.outcome == "accepted"
    before = head(tmp_path)
    d = update_document(original, init, offset_minutes=15)
    result = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(d)
    )
    assert result.payload.data.activation_status == "staged"
    assert (
        result.payload.data.preserved_target_deadline_at
        == before.request.payload.response_deadline_at
    )
    assert head(tmp_path) == before


def test_context_change_does_not_reset_a_reissued_target(tmp_path):
    original, init = initialized(tmp_path, target_case())
    d = target_document(init)
    assert execute(tmp_path, d).payload.outcome == "accepted"
    update = create_planning_updater(tmp_path, clock=lambda: NOW).execute(
        UpdatePlanningRequest.model_validate(update_document(original, init))
    )
    d["message_id"] = "renew-in-new-context"
    d["payload"]["context_id"] = update.payload.data.new_context_id
    renewed = execute(tmp_path, d)
    assert renewed.payload.outcome == "accepted", renewed
    assert renewed.payload.data.response_deadline_at == NOW + timedelta(minutes=2)


def test_target_certificate_never_advances_the_controller(tmp_path, monkeypatch):
    from oilfield_energy.workflows.single_execution.api import UnifiedSingleController

    _, init = initialized(tmp_path, target_case())

    def forbidden(*args, **kwargs):
        pytest.fail("Target admission cannot execute a device cycle")

    monkeypatch.setattr(UnifiedSingleController, "advance", forbidden)
    assert execute(tmp_path, target_document(init)).payload.outcome == "accepted"


def test_target_persistence_migrates_an_initialization_only_database(tmp_path):
    _, init = initialized(tmp_path, target_case())
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        for table in (
            "pcc_target_requests",
            "pcc_target_heads",
            "pcc_target_revalidations",
        ):
            db.execute("DROP TABLE " + table)
    assert execute(tmp_path, target_document(init)).payload.outcome == "accepted"


def test_revalidation_cannot_shorten_an_existing_guarantee(tmp_path):
    from oilfield_energy.workflows.field_initialization.target_certification import (
        certify_external_target,
    )
    from oilfield_energy.workflows.field_initialization.contracts import (
        InitializationEvidence,
        InitializationFailure,
    )

    _, init = initialized(tmp_path, target_case())
    d = target_document(init)
    assert execute(tmp_path, d).payload.outcome == "accepted"
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        current = InitializationEvidence.model_validate_json(
            db.execute("SELECT evidence FROM initialization_sessions").fetchone()[0]
        )
    shortened = current.model_copy(update={"valid_until": NOW + timedelta(minutes=4)})
    with pytest.raises(InitializationFailure, match="shorten"):
        certify_external_target(
            shortened,
            RequestPccTargetRequest.model_validate(d),
            minimum_guaranteed_until=NOW + timedelta(minutes=5),
        )


def test_concurrent_planning_activation_and_target_never_leave_stale_target_evidence(
    tmp_path,
):
    original, init = initialized(tmp_path, target_case())
    update = UpdatePlanningRequest.model_validate(update_document(original, init))
    with ThreadPoolExecutor(max_workers=2) as pool:
        target_result = pool.submit(execute, tmp_path, target_document(init))
        planning_result = pool.submit(
            create_planning_updater(tmp_path, clock=lambda: NOW).execute, update
        )
        target, planning = target_result.result(), planning_result.result()
    assert planning.payload.data.activation_status == "activated"
    if target.payload.outcome == "accepted":
        assert head(tmp_path).context_id == planning.payload.data.new_context_id
    else:
        assert target.payload.errors[0].code == "CONTEXT_CHANGED"
        assert head(tmp_path) is None


def test_equivalent_timezone_notation_keeps_the_same_target_identity(tmp_path):
    from datetime import datetime, timezone

    _, init = initialized(tmp_path, target_case())
    d = target_document(init)
    assert execute(tmp_path, d).payload.outcome == "accepted"
    d["message_id"] = "equivalent-offset"
    for key in ("effective_from", "response_deadline_at", "hold_until"):
        at = datetime.fromisoformat(d["payload"][key])
        d["payload"][key] = at.astimezone(timezone(timedelta(hours=8))).isoformat()
    assert execute(tmp_path, d).payload.outcome == "accepted"

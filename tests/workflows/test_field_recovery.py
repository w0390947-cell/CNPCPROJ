"""Explicit recovery changes current evidence without rewriting failed history."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from tests.support.field_recovery import RecoveryHarness


def test_stop_prepare_resume_and_continue_without_fabricating_a_cycle(tmp_path):
    h = RecoveryHarness(tmp_path)
    h.stop()
    prepared = h.prepare_recovery()
    assert prepared.payload.outcome == "pending", prepared
    assert prepared.payload.data.run_state == "stopped"
    assert h.query().payload.data.last_confirmed_cycle_id is None
    resumed = h.recovery_services[1].execute(h.resume_request(prepared))
    h.adopt(resumed)
    assert resumed.payload.data.run_state == "ready"
    assert resumed.payload.data.last_confirmed_cycle_id is None
    assert h.prepare().payload.outcome == "accepted"
    h.services[3].execute(h.dispatch_request())
    assert h.observe().payload.outcome == "accepted"
    assert h.report().payload.data.cycle_status == "confirmed"


def fault(h):
    h.prepare()
    h.transport.fail = True
    h.services[3].execute(h.dispatch_request())
    h.transport.fail = False
    assert h.query().payload.data.run_state == "resync_required"
    h.now += timedelta(minutes=1)
    h.minute += 1


def test_ambiguous_sent_batch_requires_verified_closure_then_resumes(tmp_path):
    h = RecoveryHarness(tmp_path)
    fault(h)
    prepared = h.prepare_recovery()
    assert prepared.payload.outcome == "pending", prepared
    resumed = h.recovery_services[1].execute(h.resume_request(prepared))
    h.adopt(resumed)
    assert resumed.payload.data.cycle_status == "unconfirmed"
    assert resumed.payload.data.batch_status == "reconciled"
    assert resumed.payload.data.last_confirmed_cycle_id is None
    assert h.prepare().payload.outcome == "accepted"
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert (
            db.execute("SELECT COUNT(*) FROM recovery_attempts WHERE consumed=1").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "change,code",
    [
        ("fence_missing", "EXECUTION_NOT_SETTLED"),
        ("verifier", "EXECUTION_NOT_SETTLED"),
        ("one_missing", "EXECUTION_NOT_SETTLED"),
        ("old_epoch", "RECOVERY_FENCE_INVALID"),
        ("task", "TASK_CHANGE_NOT_SUPPORTED"),
        ("clock", "RECOVERY_CLOCK_CHANGED"),
        ("bad_quality", "TELEMETRY_INVALID"),
    ],
)
def test_recovery_prerequisites_cannot_be_bypassed(tmp_path, change, code):
    h = RecoveryHarness(tmp_path)
    fault(h)
    request = h.recovery_request()
    raw = request.model_dump(mode="json")
    p = raw["payload"]
    if change == "fence_missing":
        p["quiescence"] = None
    elif change == "verifier":
        h.verifier.allowed = False
    elif change == "one_missing":
        p["quiescence"]["settlements"].pop()
    elif change == "old_epoch":
        p["authority"]["epoch"] = h.document["payload"]["authority"]["epoch"]
    elif change == "task":
        p["planning_inputs"]["energy_task"]["energy_min_mwh"] = 0.9
    elif change == "clock":
        p["planning_inputs"]["effective_from"] = h.now.isoformat()
    elif change == "bad_quality":
        p["snapshot"]["device_states"][0]["p_mw"]["quality"] = "bad"
    result = h.prepare_recovery(type(request).model_validate(raw))
    assert result.payload.errors[0].code == code, result
    assert h.query().payload.data.run_state == "resync_required"
    assert h.query().payload.data.last_confirmed_cycle_id is None


def test_new_event_invalidates_prepared_recovery(tmp_path):
    h = RecoveryHarness(tmp_path)
    h.stop()
    prepared = h.prepare_recovery()
    assert prepared.payload.outcome == "pending"
    h.stop()  # Same state name, but an explicitly newer stop reason/time is also guarded.
    result = h.recovery_services[1].execute(h.resume_request(prepared))
    assert result.payload.outcome == "rejected"
    assert result.payload.errors[0].code == "RECOVERY_STALE"
    assert h.query().payload.data.run_state == "stopped"


def test_concurrent_resume_and_replay_commit_once(tmp_path):
    h = RecoveryHarness(tmp_path)
    h.stop()
    prepared = h.prepare_recovery()
    request = h.resume_request(prepared)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: h.recovery_services[1].execute(request), range(2)))
    assert results[0] == results[1]
    assert results[0].payload.data.run_state == "ready", results
    again = h.resume_request(prepared)
    assert (
        h.recovery_services[1].execute(again).payload.errors[0].code == "RECOVERY_ALREADY_CONSUMED"
    )


def test_resume_rechecks_current_authority_without_consuming_candidate(tmp_path):
    h = RecoveryHarness(tmp_path)
    h.stop()
    prepared = h.prepare_recovery()
    request = h.resume_request(prepared)
    h.transport.allowed = False
    result = h.recovery_services[1].execute(request)
    assert result.payload.errors[0].code == "AUTHORITY_LOST", result
    assert h.query().payload.data.run_state == "stopped"
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT consumed FROM recovery_attempts").fetchone()[0] == 0
    h.transport.allowed = True
    assert (
        h.recovery_services[1].execute(h.resume_request(prepared)).payload.data.run_state == "ready"
    )


@pytest.mark.parametrize("verification", ["absent", "unavailable", "revoked"])
def test_claimed_recovery_requires_available_trusted_verification(tmp_path, verification):
    from oilfield_energy.bootstrap.field_initialization import create_recovery_services

    h = RecoveryHarness(tmp_path)
    fault(h)
    prepared = h.prepare_recovery()
    request = h.resume_request(prepared)

    class UnavailableVerifier:
        def verify_quiescence(self, grid, authority, evidence):
            raise OSError("trusted evidence service unavailable")

    verifier = None if verification == "absent" else UnavailableVerifier()
    if verification == "revoked":
        h.verifier.allowed = False
        verifier = h.verifier
    resumer = create_recovery_services(
        tmp_path, clock=lambda: h.now, transport=h.transport, verifier=verifier
    )[1]
    result = resumer.execute(request)
    expected = (
        "RECOVERY_VERIFICATION_UNAVAILABLE"
        if verification == "unavailable"
        else "EXECUTION_NOT_SETTLED"
    )
    assert result.payload.errors[0].code == expected, result
    assert h.query().payload.data.run_state == "resync_required"
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT consumed FROM recovery_attempts").fetchone()[0] == 0


def test_active_target_after_original_deadline_is_preserved(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_pcc_target_requester
    from oilfield_energy.workflows.field_initialization.contracts import RequestPccTargetRequest
    from tests.support.field_targets import target_case, target_document

    h = RecoveryHarness(tmp_path, target_case())
    target = target_document(h.initial)
    target["payload"]["target_p_import_mw"] = 0.8
    accepted = create_pcc_target_requester(tmp_path, clock=lambda: h.now).execute(
        RequestPccTargetRequest.model_validate(target)
    )
    assert accepted.payload.outcome == "accepted"
    for _ in range(3):
        h.prepare()
        h.services[3].execute(h.dispatch_request())
        h.observe()
        assert h.report().payload.data.cycle_status == "confirmed"
    h.stop()
    prepared = h.prepare_recovery()
    assert prepared.payload.outcome == "pending", prepared
    resumed = h.recovery_services[1].execute(h.resume_request(prepared))
    h.adopt(resumed)
    assert (
        resumed.payload.data.target_response_deadline_at
        == accepted.payload.data.response_deadline_at
    )
    assert resumed.payload.data.target_guaranteed_until == accepted.payload.data.guaranteed_until
    assert h.prepare().payload.outcome == "accepted"
    h.services[3].execute(h.dispatch_request())
    h.observe()
    assert h.report().payload.data.cycle_status == "confirmed"


@pytest.mark.parametrize("completed", [False, True])
def test_elapsed_task_needs_an_existing_confirmed_outcome(tmp_path, completed):
    from tests.support.field_initialization import NOW, request_document

    document = request_document()
    document["payload"]["planning_inputs"]["energy_task"]["deadline_at"] = (
        NOW + timedelta(minutes=1)
    ).isoformat()
    h = RecoveryHarness(tmp_path, document)
    if completed:
        h.prepare()
        h.services[3].execute(h.dispatch_request())
        h.observe()
        assert h.report().payload.data.energy_task_outcome == "met"
    else:
        h.now += timedelta(minutes=1)
        h.minute += 1
    h.stop()
    h.now += timedelta(minutes=1)
    h.minute += 1
    prepared = h.prepare_recovery()
    if not completed:
        assert prepared.payload.errors[0].code == "TASK_DEADLINE_PASSED"
    else:
        assert prepared.payload.outcome == "pending", prepared
        resumed = h.recovery_services[1].execute(h.resume_request(prepared))
        h.adopt(resumed)
        assert resumed.payload.data.energy_task_outcome == "met"
        assert h.prepare().payload.outcome == "accepted"


def test_expired_target_is_archived_without_blocking_safety_recovery(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_pcc_target_requester
    from oilfield_energy.workflows.field_initialization.contracts import RequestPccTargetRequest
    from tests.support.field_targets import target_case, target_document

    h = RecoveryHarness(tmp_path, target_case())
    target = target_document(h.initial)
    r = create_pcc_target_requester(tmp_path, clock=lambda: h.now).execute(
        RequestPccTargetRequest.model_validate(target)
    )
    assert r.payload.outcome == "accepted"
    h.stop()
    h.now += timedelta(minutes=5)
    h.minute += 5
    prepared = h.prepare_recovery()
    assert prepared.payload.outcome == "pending", prepared
    resumed = h.recovery_services[1].execute(h.resume_request(prepared))
    h.adopt(resumed)
    assert resumed.payload.data.target_id is None
    assert resumed.payload.data.recovery_expired_target_id == "external-1"
    assert h.prepare().payload.outcome == "accepted"


def test_resume_transaction_rolls_back_all_heads_and_can_retry(tmp_path, monkeypatch):
    import oilfield_energy.workflows.field_initialization.adapters.execution_storage as storage

    h = RecoveryHarness(tmp_path)
    h.stop()
    prepared = h.prepare_recovery()
    request = h.resume_request(prepared)
    original = storage.save_ledger

    def unavailable(db, run, ledger):
        original(db, run, ledger)
        raise sqlite3.OperationalError("injected persistence failure")

    with monkeypatch.context() as m:
        m.setattr(storage, "save_ledger", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            h.recovery_services[1].execute(request)
    assert h.query().payload.data.run_state == "stopped"
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT consumed FROM recovery_attempts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM recovery_heads").fetchone()[0] == 0
    assert h.recovery_services[1].execute(request).payload.data.run_state == "ready"


def test_resume_expiration_and_new_snapshot_guard(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_snapshot_submitter
    from oilfield_energy.workflows.field_initialization.contracts import SubmitSnapshotRequest

    h = RecoveryHarness(tmp_path)
    h.stop()
    prepared = h.prepare_recovery()
    newer = h.proposal.payload.snapshot.model_copy(
        update={"snapshot_id": "newer", "sequence": h.sequence + 1}
    )
    snapshot = SubmitSnapshotRequest.model_validate(h.envelope("SubmitSnapshot", newer))
    create_snapshot_submitter(tmp_path, clock=lambda: h.now).execute(snapshot)
    assert (
        h.recovery_services[1].execute(h.resume_request(prepared)).payload.errors[0].code
        == "RECOVERY_STALE"
    )
    h.sequence = newer.sequence
    latest = h.prepare_recovery(h.recovery_request("recovery-2"))
    assert latest.payload.outcome == "pending", latest
    h.now += timedelta(seconds=31)
    assert (
        h.recovery_services[1].execute(h.resume_request(latest)).payload.errors[0].code
        == "RECOVERY_EXPIRED"
    )


def test_post_recovery_late_application_violating_fence_reblocks_run(tmp_path):
    from oilfield_energy.workflows.field_initialization.contracts import (
        CommandResult,
        ReportExecutionRequest,
    )

    h = RecoveryHarness(tmp_path)
    fault(h)
    old = h.transport.sent[0]
    prepared = h.prepare_recovery()
    h.adopt(h.recovery_services[1].execute(h.resume_request(prepared)))
    h.now += timedelta(seconds=1)
    c = old.commands[0]
    report = ReportExecutionRequest.model_validate(
        h.envelope(
            "ReportExecution",
            dict(
                context_id=old.evidence.domain.context_id,
                cycle_id=old.evidence.request.payload.cycle_id,
                command_batch_id=old.evidence.command_batch_id,
                feedback_stage="device_ack",
                reported_at=h.now,
                command_results=[
                    CommandResult(
                        command_id=c.command_id,
                        resource_id=c.resource_id,
                        transport_status="accepted",
                        device_status="applied",
                        acknowledged_at=h.now,
                        applied_at=h.now,
                        echoed_p_setpoint_mw=c.p_setpoint_mw,
                        echoed_q_setpoint_mvar=c.q_setpoint_mvar,
                        reason="late",
                    )
                ],
                observed_snapshot_id=None,
                energy_evidence=None,
                reason="",
            ),
        )
    )
    r = h.services[0].execute(report)
    assert r.payload.errors[0].code == "RECOVERY_FENCE_VIOLATED", r
    assert h.query().payload.data.run_state == "resync_required"


def test_partial_device_rejection_is_settled_per_command(tmp_path):
    h = RecoveryHarness(tmp_path)
    h.transport.transport_only = True
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    results = list(h.transport.results)
    results[0] = results[0].model_copy(update={"device_status": "rejected"})
    assert h.report("device_ack", results).payload.data.run_state == "resync_required"
    h.now += timedelta(minutes=1)
    h.minute += 1
    request = h.recovery_request()
    raw = request.model_dump(mode="json")
    raw["payload"]["quiescence"]["settlements"][0]["disposition"] = "rejected"
    prepared = h.prepare_recovery(type(request).model_validate(raw))
    assert prepared.payload.outcome == "pending", prepared
    resumed = h.recovery_services[1].execute(h.resume_request(prepared))
    assert resumed.payload.data.batch_status == "reconciled"
    assert resumed.payload.data.last_confirmed_cycle_id is None


def test_completed_task_remains_recorded_during_later_planning_update(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_planning_updater
    from oilfield_energy.workflows.field_initialization.contracts import UpdatePlanningRequest
    from tests.support.field_initialization import NOW, request_document
    from tests.support.field_planning import update_document

    document = request_document()
    document["payload"]["planning_inputs"]["energy_task"]["deadline_at"] = (
        NOW + timedelta(minutes=1)
    ).isoformat()
    h = RecoveryHarness(tmp_path, document)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    assert h.report().payload.data.energy_task_outcome == "met"
    h.stop()
    h.now += timedelta(minutes=1)
    h.minute += 1
    prepared = h.prepare_recovery()
    h.adopt(h.recovery_services[1].execute(h.resume_request(prepared)))
    update = update_document(h.document, h.initial, offset_minutes=2)
    update["created_at"] = h.now
    r = create_planning_updater(tmp_path, clock=lambda: h.now).execute(
        UpdatePlanningRequest.model_validate(update)
    )
    assert r.payload.data.activation_status == "activated", r
    assert r.payload.data.energy_task_guarantee == "maintained"
    assert h.query().payload.data.energy_task_outcome == "met"


def test_recovery_identity_and_historical_replay_do_not_reactivate(tmp_path):
    h = RecoveryHarness(tmp_path)
    h.stop()
    prepared = h.prepare_recovery()
    original = h.proposal
    h.adopt(h.recovery_services[1].execute(h.resume_request(prepared)))
    assert h.recovery_services[0].execute(original) == prepared
    assert h.query().payload.data.run_state == "ready"
    raw = original.model_dump()
    raw["message_id"] = "different-envelope"
    raw["payload"]["reason"] = "different immutable proposal"
    assert (
        h.recovery_services[0].execute(type(original).model_validate(raw)).payload.errors[0].code
        == "RECOVERY_ID_CONFLICT"
    )


def test_shadow_recovery_preserves_no_write_permission(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import (
        create_execution_services,
        create_recovery_services,
    )
    from oilfield_energy.workflows.field_initialization.contracts import (
        PrepareResynchronizationRequest,
        ResumeRunRequest,
        StopRunRequest,
    )
    from tests.support.field_initialization import NOW
    from tests.support.field_planning import initialized
    from tests.support.field_snapshots import snapshot_document

    original, init = initialized(tmp_path)

    def envelope(op, payload):
        return dict(
            interface_version=init.interface_version,
            operation=op,
            message_id=op,
            correlation_id=None,
            microgrid_id=init.microgrid_id,
            run_id=init.run_id,
            created_at=NOW,
            payload=payload,
        )

    create_execution_services(tmp_path, clock=lambda: NOW)[2].execute(
        StopRunRequest.model_validate(
            envelope(
                "StopRun",
                dict(reason="stop", requested_at=NOW, authority=original["payload"]["authority"]),
            )
        )
    )
    preparer, resumer = create_recovery_services(tmp_path, clock=lambda: NOW)
    request = PrepareResynchronizationRequest.model_validate(
        envelope(
            "PrepareResynchronization",
            dict(
                context_id=init.payload.data.context_id,
                resynchronization_id="shadow-recovery",
                pending_batch_id=None,
                snapshot=snapshot_document(original, init)["payload"],
                planning_inputs=original["payload"]["planning_inputs"],
                authority=original["payload"]["authority"],
                quiescence=None,
                reason="explicit shadow recovery",
            ),
        )
    )
    prepared = preparer.execute(request)
    assert prepared.payload.outcome == "pending", prepared
    resumed = resumer.execute(
        ResumeRunRequest.model_validate(
            envelope(
                "ResumeRun",
                dict(
                    context_id=init.payload.data.context_id,
                    resynchronization_id="shadow-recovery",
                    proposed_context_id=prepared.payload.data.proposed_context_id,
                    authority=original["payload"]["authority"],
                ),
            )
        )
    )
    assert resumed.payload.outcome == "accepted" and resumed.payload.data.mode == "shadow", resumed

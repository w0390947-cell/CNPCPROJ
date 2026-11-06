"""Measured-state progression, ambiguous effects and durable execution facts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from oilfield_energy.bootstrap.field_initialization import create_pcc_target_requester
from oilfield_energy.workflows.field_initialization.contracts import (
    QueryRunStatusRequest,
    ReportExecutionRequest,
    RequestPccTargetRequest,
)
from tests.support.field_execution import Harness
from tests.support.field_targets import target_case, target_document


def test_three_observed_cycles_advance_once_and_restart(tmp_path):
    h = Harness(tmp_path)
    for k in range(3):
        p = h.prepare()
        assert p.payload.data.decision == "dispatchable", p
        request = h.dispatch_request()
        sent = h.services[3].execute(request)
        assert sent.payload.outcome == "pending", sent
        assert h.query().payload.data.last_confirmed_cycle_id == (f"cycle-{k - 1}" if k else None)
        assert h.services[3].execute(request) == sent
        assert len(h.transport.sent) == k + 1
        observed = h.observe()
        assert observed.payload.outcome == "accepted", observed
        assert not observed.payload.data.resync_required
        confirmed = h.report()
        assert confirmed.payload.data.cycle_status == "confirmed", confirmed
        assert confirmed.payload.data.last_confirmed_cycle_id == f"cycle-{k}"
        assert h.query().payload.data.confirmed_snapshot_id == h.snapshot_id
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM execution_batches").fetchone()[0] == 3


def test_external_target_deadline_survives_actual_cycles(tmp_path):
    h = Harness(tmp_path, target_case())
    target = target_document(h.initial)
    target["payload"]["target_p_import_mw"] = 0.8
    result = create_pcc_target_requester(tmp_path, clock=lambda: h.now).execute(
        RequestPccTargetRequest.model_validate(target)
    )
    assert result.payload.outcome == "accepted", result
    for k in range(3):
        assert h.prepare().payload.outcome == "accepted"
        h.services[3].execute(h.dispatch_request())
        assert h.observe().payload.outcome == "accepted"
        r = h.report()
        assert r.payload.data.cycle_status == "confirmed", r
        assert (
            r.payload.data.target_response_deadline_at == result.payload.data.response_deadline_at
        )
        assert r.payload.data.target_guaranteed_until == result.payload.data.guaranteed_until
    assert any(abs(x) > 0.01 for values in h.state.values() for x in values)


@pytest.mark.parametrize(
    "failure", ["partial", "timeout", "state", "transport", "late_apply", "echo"]
)
def test_uncertain_execution_blocks_without_replacing_confirmed_state(tmp_path, failure):
    h = Harness(tmp_path)
    h.prepare()
    h.transport.fail = failure == "transport"
    h.services[3].execute(h.dispatch_request())
    results = list(h.transport.results)
    if failure == "partial":
        results[0] = results[0].model_copy(update={"device_status": "rejected"})
        r = h.report("device_ack", results)
    elif failure == "transport":
        r = h.query()
    else:
        h.observe(corrupt=failure == "state")
        if failure == "timeout":
            h.now += timedelta(seconds=6)
        elif failure == "late_apply":
            results[0] = results[0].model_copy(
                update={"applied_at": results[0].applied_at + timedelta(seconds=1)}
            )
        elif failure == "echo":
            results[0] = results[0].model_copy(update={"echoed_p_setpoint_mw": 0.2})
        r = h.report(results=results)
    assert r.payload.data.run_state == "resync_required", r
    assert r.payload.data.last_confirmed_cycle_id is None
    h.minute += 100  # A new cycle request; historical preparation replay stays immutable.
    assert h.prepare().payload.outcome == "rejected"
    assert h.query().payload.data.run_state == "resync_required"


def test_stop_before_send_and_inflight_feedback(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    request = h.dispatch_request()
    h.stop()
    assert h.services[3].execute(request).payload.outcome == "rejected"
    assert not h.transport.sent


def test_stop_after_send_still_records_actual_completion(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.stop()
    h.observe()
    r = h.report()
    assert r.payload.data.run_state == "stopped", r
    assert r.payload.data.cycle_status == "confirmed", r
    h.minute += 100
    assert h.prepare().payload.outcome == "rejected"


def test_concurrent_duplicate_dispatch_never_repeats_external_effect(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    request = h.dispatch_request()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: h.services[3].execute(request), range(2)))
    assert results[0] == results[1]
    assert len(h.transport.sent) == 1


def test_transport_acceptance_is_not_completion(tmp_path):
    h = Harness(tmp_path)
    h.transport.transport_only = True
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    state = h.query().payload.data
    assert state.run_state == "awaiting_feedback" and state.last_confirmed_cycle_id is None
    h.observe()
    r = h.report()
    assert r.payload.outcome == "rejected"
    assert r.payload.errors[0].code == "PARTIAL_OR_UNKNOWN_EXECUTION"


def test_confirmed_historical_feedback_does_not_regress_new_boundary(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    original = h.prepared
    payload = dict(
        context_id=h.initial.payload.data.context_id,
        cycle_id=original.payload.data.cycle_id,
        command_batch_id=original.payload.data.command_batch_id,
        feedback_stage="cycle_end",
        reported_at=h.now,
        command_results=h.transport.results,
        observed_snapshot_id=h.snapshot_id,
        energy_evidence=None,
        reason="late duplicate",
    )
    assert h.report().payload.data.cycle_status == "confirmed"
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    assert h.report().payload.data.cycle_status == "confirmed"
    before = h.query().payload.data
    h.now += timedelta(seconds=6)
    r = h.services[0].execute(
        ReportExecutionRequest.model_validate(h.envelope("ReportExecution", payload))
    )
    assert r.payload.outcome == "accepted", r
    assert r.payload.data.last_confirmed_cycle_id == before.last_confirmed_cycle_id
    assert r.payload.data.confirmed_snapshot_id == before.confirmed_snapshot_id
    assert r.payload.data.run_state == "ready"


def test_concurrent_confirmation_commits_one_boundary(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: h.report(), range(2)))
    assert all(r.payload.data.cycle_status == "confirmed" for r in results), results
    assert h.query().payload.data.last_confirmed_cycle_id == "cycle-0"


def test_ambiguous_send_is_queried_after_restart_without_resending(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    h.transport.fail = True
    request = h.dispatch_request()
    h.services[3].execute(request)
    h.transport.fail = False
    assert h.query(reconcile=True).payload.data.run_state == "resync_required"
    h.services[3].execute(request)
    assert len(h.transport.sent) == 1


def test_send_claim_survives_crash_before_transport_invocation(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.prepare()
    request = h.dispatch_request()
    service = h.services[3]

    def crash(*args):
        raise SystemExit("simulated process termination")

    monkeypatch.setattr(h.transport, "send", crash)
    with pytest.raises(SystemExit):
        service.execute(request)
    assert h.query().payload.data.batch_status == "sending"
    h.services[3].execute(request)
    assert not h.transport.sent
    h.now += timedelta(seconds=6)
    assert h.query().payload.data.run_state == "resync_required"


def test_failed_state_commit_rolls_back_confirmation(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    import oilfield_energy.workflows.field_initialization.adapters.execution_storage as storage

    real = storage.save_ledger

    def fail(db, run, ledger):
        real(db, run, ledger)
        raise sqlite3.OperationalError("injected disk failure")

    with monkeypatch.context() as m:
        m.setattr(storage, "save_ledger", fail)
        with pytest.raises(sqlite3.OperationalError):
            h.report()
    assert h.query().payload.data.last_confirmed_cycle_id is None
    assert h.report().payload.data.cycle_status == "confirmed"


def test_query_message_conflict_and_other_operation_namespace(tmp_path):
    h = Harness(tmp_path)
    request = QueryRunStatusRequest.model_validate(h.envelope("QueryRunStatus", {}))
    result = h.services[1].execute(request)
    assert h.services[1].execute(request) == result
    changed = request.model_copy(
        update={"payload": request.payload.model_copy(update={"reconcile": True})}
    )
    assert h.services[1].execute(changed).payload.errors[0].code == "DUPLICATE_ID_CONFLICT"
    from oilfield_energy.bootstrap.field_initialization import create_snapshot_submitter
    from oilfield_energy.workflows.field_initialization.contracts import SubmitSnapshotRequest
    from tests.support.field_snapshots import snapshot_document

    doc = snapshot_document(h.document, h.initial)
    doc["message_id"] = request.message_id
    r = create_snapshot_submitter(tmp_path, clock=lambda: h.now).execute(
        SubmitSnapshotRequest.model_validate(doc)
    )
    assert r.payload.errors[0].code == "DUPLICATE_ID_CONFLICT"


def test_removed_port_cannot_prepare_an_existing_closed_loop_run(tmp_path):
    h = Harness(tmp_path)
    from oilfield_energy.bootstrap.field_initialization import create_control_cycle_preparer
    from oilfield_energy.workflows.field_initialization.contracts import PrepareControlCycleRequest
    from tests.support.field_cycles import cycle_document

    r = create_control_cycle_preparer(tmp_path, clock=lambda: h.now).execute(
        PrepareControlCycleRequest.model_validate(cycle_document(h.document, h.initial))
    )
    assert r.payload.errors[0].code == "TRANSPORT_UNAVAILABLE"


def test_planning_activation_uses_later_measured_boundary_and_preserves_target(tmp_path):
    h = Harness(tmp_path, target_case())
    t = target_document(h.initial)
    t["payload"]["target_p_import_mw"] = 0.8
    accepted = create_pcc_target_requester(tmp_path, clock=lambda: h.now).execute(
        RequestPccTargetRequest.model_validate(t)
    )
    assert accepted.payload.outcome == "accepted"
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    assert h.report().payload.data.cycle_status == "confirmed"
    from oilfield_energy.bootstrap.field_initialization import create_planning_updater
    from oilfield_energy.workflows.field_initialization.contracts import UpdatePlanningRequest
    from tests.support.field_planning import update_document

    document = update_document(h.document, h.initial, offset_minutes=1)
    document["created_at"] = h.now
    result = create_planning_updater(tmp_path, clock=lambda: h.now).execute(
        UpdatePlanningRequest.model_validate(document)
    )
    assert result.payload.data.activation_status == "activated", result
    assert (
        result.payload.data.preserved_target_deadline_at
        == accepted.payload.data.response_deadline_at
    )
    context = result.payload.data.new_context_id
    h.initial = h.initial.model_copy(
        update={
            "payload": h.initial.payload.model_copy(
                update={"data": h.initial.payload.data.model_copy(update={"context_id": context})}
            )
        }
    )
    assert h.prepare().payload.outcome == "accepted"
    h.services[3].execute(h.dispatch_request())
    assert h.observe().payload.outcome == "accepted"
    r = h.report()
    assert r.payload.data.cycle_status == "confirmed", r
    assert r.payload.data.target_response_deadline_at == accepted.payload.data.response_deadline_at


def test_original_energy_task_outcome_is_retained_after_its_deadline(tmp_path):
    from tests.support.field_initialization import NOW, request_document

    document = request_document()
    document["payload"]["planning_inputs"]["energy_task"]["deadline_at"] = (
        NOW + timedelta(minutes=2)
    ).isoformat()
    h = Harness(tmp_path, document)
    for k in range(3):
        h.prepare()
        h.services[3].execute(h.dispatch_request())
        h.observe()
        r = h.report()
        assert r.payload.data.cycle_status == "confirmed", r
        assert r.payload.data.energy_task_outcome == ("pending" if k == 0 else "met")


def test_late_dispatch_is_withheld_and_does_not_send(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    h.now += timedelta(seconds=1)
    r = h.services[3].execute(h.dispatch_request())
    assert r.payload.outcome == "rejected"
    assert r.payload.data.run_state == "resync_required"
    assert not h.transport.sent


def test_reconciliation_does_not_hold_the_session_write_transaction(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())

    def query(grid, batch):
        # A second service must be able to commit while the transport is queried.
        assert h.stop().payload.outcome == "accepted"
        return h.transport.results

    monkeypatch.setattr(h.transport, "query", query)
    r = h.query(reconcile=True)
    assert r.payload.data.run_state == "stopped", r
    assert len(h.transport.sent) == 1


def test_authority_revoked_after_claim_prevents_transport_invocation(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.prepare()
    calls = 0

    def authorized(grid, authority):
        nonlocal calls
        calls += 1
        return calls == 1  # Verified for the claim; revoked before the write.

    monkeypatch.setattr(h.transport, "authorized", authorized)
    h.services[3].execute(h.dispatch_request())
    assert not h.transport.sent
    assert h.query().payload.data.run_state == "resync_required"


def test_unsupported_energy_uncertainty_cannot_confirm_exact_state(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    r = h.report(
        energy_evidence=dict(
            method="measured_energy",
            interval_start=h.now - timedelta(minutes=1),
            interval_end=h.now,
            source_ref="resource:storage:energy",
            energy_end_mwh=h.energy,
            uncertainty_mwh=0.01,
        )
    )
    assert r.payload.errors[0].code == "ENERGY_EVIDENCE_INVALID"
    assert r.payload.data.last_confirmed_cycle_id is None


def test_cycle_confirmation_budget_is_rechecked_before_commit(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    from oilfield_energy.workflows.field_initialization.adapters.execution_storage import (
        SqliteExecution,
    )
    from oilfield_energy.workflows.field_initialization.api import ReportExecution

    ticks = iter([0.0, 31.0])
    timed = ReportExecution(
        SqliteExecution(tmp_path / "sessions.sqlite3"), lambda: h.now, elapsed=lambda: next(ticks)
    )
    p = dict(
        context_id=h.initial.payload.data.context_id,
        cycle_id="cycle-0",
        command_batch_id=h.prepared.payload.data.command_batch_id,
        feedback_stage="cycle_end",
        reported_at=h.now,
        command_results=h.transport.results,
        observed_snapshot_id=h.snapshot_id,
        energy_evidence=dict(
            method="measured_energy",
            interval_start=h.now - timedelta(minutes=1),
            interval_end=h.now,
            source_ref="resource:storage:energy",
            energy_end_mwh=h.energy,
            uncertainty_mwh=0.0,
        ),
        reason="",
    )
    r = timed.execute(ReportExecutionRequest.model_validate(h.envelope("ReportExecution", p)))
    assert r.payload.errors[0].code == "COMPUTE_BUDGET_EXCEEDED"
    assert r.payload.data.last_confirmed_cycle_id is None

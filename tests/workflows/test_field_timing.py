"""Real wall-clock offsets with an independent analytic plant and no vendor I/O."""

from datetime import timedelta
from fractions import Fraction as F

import pytest

from tests.support.field_initialization import NOW
from tests.support.field_timing import (
    TimedHarness,
    TimedTransport,
    closed_loop_document,
    timing_document,
)


def test_three_delayed_asynchronous_cycles_use_measured_state(tmp_path):
    h = TimedHarness(tmp_path)
    for k in range(3):
        p = h.prepare()
        assert p.payload.data.decision == "dispatchable", p
        assert p.payload.data.apply_at == NOW + timedelta(minutes=k, seconds=10)
        h.now += timedelta(seconds=1)
        assert h.services[3].execute(h.dispatch_request()).payload.outcome == "pending"
        assert h.query().payload.data.last_confirmed_cycle_id == (f"cycle-{k - 1}" if k else None)
        assert h.observe().payload.outcome == "accepted"
        completed = h.report()
        assert completed.payload.data.cycle_status == "confirmed", completed
        assert completed.payload.data.validation_scope == "bounded_monotone_v1"
        assert completed.payload.data.confirmed_state.energy_mwh == h.energy
        assert h.query().payload.data.confirmed_snapshot_id == h.snapshot_id


@pytest.mark.parametrize(
    "offsets,code",
    [
        ((9, 14), "EXECUTION_TIMING_UNSUPPORTED"),
        ((12, 17), "EXECUTION_TIMING_UNSUPPORTED"),
        ((10, 16), "EXECUTION_TIMING_UNSUPPORTED"),
    ],
)
def test_out_of_window_or_excess_skew_cannot_confirm(tmp_path, offsets, code):
    h = TimedHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe(offsets)
    r = h.report()
    assert r.payload.errors[0].code == code, r.payload.errors
    assert h.query().payload.data.run_state == "resync_required"
    assert h.query().payload.data.last_confirmed_cycle_id is None


def test_late_receipt_of_in_window_application_is_valid(tmp_path):
    h = TimedHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    h.now += timedelta(seconds=2)
    assert h.report().payload.data.cycle_status == "confirmed"
    assert h.prepare().payload.outcome == "accepted"


def test_missed_dispatch_window_never_calls_transport(tmp_path):
    h = TimedHarness(tmp_path)
    h.prepare()
    h.now = NOW + timedelta(seconds=9)
    r = h.services[3].execute(h.dispatch_request())
    assert r.payload.errors[0].code == "COMMAND_WITHHELD"
    assert not h.transport.sent
    assert h.query().payload.data.run_state == "resync_required"


def test_storage_measurement_uses_old_and_new_actual_intervals(tmp_path):
    h = TimedHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    r = h.report()
    actual = float(F(1.0) - F(0.1) / F(0.95) * 14 / 3600)
    assert r.payload.data.confirmed_state.energy_mwh == actual
    assert actual != 1.0  # A full minute at the new zero setpoint would fabricate this value.
    assert (
        r.payload.data.confirmed_state.energy_mwh
        != h.prepared.payload.data.predicted_endpoint.storage_energy_mwh
    )


@pytest.mark.parametrize("window", [(10.0, 16.0), (10.1, 16.2)])
def test_window_reachability_encloses_switch_time_and_charge_reversal(tmp_path, window):
    from oilfield_energy.modules.control.api import point_box, tube_contains
    from oilfield_energy.modules.control.contracts import TubeCommand, TubeState
    from oilfield_energy.workflows.field_initialization.contracts import QueryRunStatusRequest
    from oilfield_energy.workflows.field_initialization.timing_certification import timed_transition

    h = TimedHarness(tmp_path)
    c = (
        h.services[1]
        .store.inspect(QueryRunStatusRequest.model_validate(h.envelope("QueryRunStatus", {})))
        .current
    )
    timing = c.request.payload.control_policy.bounded_timing.model_copy(
        update={"apply_not_before_seconds": window[0], "apply_not_after_seconds": window[1]}
    )
    path, end = timed_transition(
        c.domain,
        point_box(c.initial),
        TubeCommand(p_mw=(0.3, -0.1), q_mvar=(0.0, 0.0)),
        0,
        timing,
    )
    for switch in [F(round(x * 1_000_000), 1_000_000) for x in (*window, sum(window) / 2)]:
        for second in range(61):
            before = min(second, switch)
            after = max(0, second - switch)
            energy = float(
                F(1) - F(0.1) / F(0.95) * before / 3600 + F(0.1) * F(0.95) * after / 3600
            )
            state = TubeState(
                p_mw=(0.2 if second < switch else 0.3, 0.1 if second < switch else -0.1),
                q_mvar=(0.0, 0.0),
                energy_mwh=energy,
            )
            assert tube_contains(path, state)
            if second == 60:
                assert tube_contains(end, state)
    assert path.energy_mwh.low < 1 < end.energy_mwh.low


def test_independent_device_order_is_checked_even_when_both_endpoints_are_safe(
    tmp_path, monkeypatch
):
    import oilfield_energy.workflows.field_initialization.timing_certification as timing
    from oilfield_energy.modules.control.api import point_box
    from oilfield_energy.modules.control.contracts import TubeCommand, TubeState
    from oilfield_energy.workflows.field_initialization.contracts import (
        InitializationFailure,
        QueryRunStatusRequest,
    )
    from oilfield_energy.workflows.single_execution.api import unified_network_box

    h = TimedHarness(tmp_path)
    c = (
        h.services[1]
        .store.inspect(QueryRunStatusRequest.model_validate(h.envelope("QueryRunStatus", {})))
        .current
    )
    domain = c.domain.model_copy(
        update={"network": c.domain.network.model_copy(update={"pcc_min_mw": 0.3})}
    )
    old = TubeState(p_mw=(0.0, 0.4), q_mvar=(0.0, 0.0), energy_mwh=1.0)
    new = old.model_copy(update={"p_mw": (0.4, 0.0)})
    for state in (old, new):
        assert unified_network_box(domain, point_box(state), domain.forecasts[0]).certified
    monkeypatch.setattr(
        timing, "candidate_command", lambda *args: TubeCommand(p_mw=new.p_mw, q_mvar=new.q_mvar)
    )
    with pytest.raises(InitializationFailure) as error:
        timing.certify_timed_continuation(c.model_copy(update={"domain": domain, "initial": old}))
    assert error.value.error.code == "TIMED_SAFETY_UNPROVEN"


@pytest.mark.parametrize("change", ["response", "ids", "hold", "unsettled", "revision"])
def test_incomplete_timing_or_hold_contract_cannot_admit(tmp_path, change):
    from oilfield_energy.bootstrap.field_initialization import create_field_initializer
    from oilfield_energy.workflows.field_initialization.contracts import InitializeRequest

    d = closed_loop_document(timing_document())
    p = d["payload"]
    if change == "response":
        p["control_policy"]["bounded_timing"]["resources"][1]["settling_max_seconds"] = 1.0
    elif change == "ids":
        p["control_policy"]["bounded_timing"]["resources"][0]["resource_id"] = "unknown"
    elif change == "hold":
        p["initial_snapshot"]["held_setpoints"] = None
    elif change == "unsettled":
        p["initial_snapshot"]["held_setpoints"][0]["p_mw"] = 0.3
    else:
        p["control_policy"]["bounded_timing"]["revision"] = "different"
    r = create_field_initializer(tmp_path, clock=lambda: NOW, transport=TimedTransport()).execute(
        InitializeRequest.model_validate(d)
    )
    assert r.payload.outcome == "rejected"
    expected = {
        "response": "TIMED_RESPONSE_UNPROVEN",
        "ids": "IDENTITY_MISMATCH",
        "hold": "HOLD_EVIDENCE_REQUIRED",
        "unsettled": "TIMED_BOUNDARY_UNSETTLED",
        "revision": "AUTHORITY_LOST",
    }
    assert r.payload.errors[0].code == expected[change], r.payload.errors


def test_target_deadline_and_planning_switch_survive_delayed_cycles(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import (
        create_pcc_target_requester,
        create_planning_updater,
    )
    from oilfield_energy.workflows.field_initialization.contracts import (
        RequestPccTargetRequest,
        UpdatePlanningRequest,
    )
    from tests.support.field_planning import update_document
    from tests.support.field_targets import target_document

    d = timing_document(False)
    d["payload"]["model_package"]["devices"][0].update(
        q_min_mvar=-0.3, q_max_mvar=0.3, q_control_enabled=True
    )
    h = TimedHarness(tmp_path, d)
    t = target_document(h.initial)
    t["created_at"] = h.now
    t["payload"]["target_p_import_mw"] = 0.8
    accepted = create_pcc_target_requester(tmp_path, clock=lambda: h.now).execute(
        RequestPccTargetRequest.model_validate(t)
    )
    assert accepted.payload.outcome == "accepted", accepted.payload.errors
    for _ in range(3):
        assert h.prepare().payload.outcome == "accepted"
        h.services[3].execute(h.dispatch_request())
        assert h.observe().payload.outcome == "accepted"
        r = h.report()
        assert r.payload.data.cycle_status == "confirmed", r.payload.errors
        assert (
            r.payload.data.target_response_deadline_at == accepted.payload.data.response_deadline_at
        )
    update = update_document(h.document, h.initial, offset_minutes=3)
    update["created_at"] = h.now
    r = create_planning_updater(tmp_path, clock=lambda: h.now).execute(
        UpdatePlanningRequest.model_validate(update)
    )
    assert r.payload.data.activation_status == "activated", r.payload.errors
    assert h.query().payload.data.target_guaranteed_until == accepted.payload.data.guaranteed_until


def test_delayed_storage_cannot_reuse_a_synchronous_task_certificate(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_field_initializer
    from oilfield_energy.workflows.field_initialization.contracts import InitializeRequest

    d = closed_loop_document(timing_document())
    d["payload"]["planning_inputs"]["energy_task"].update(
        deadline_at=NOW + timedelta(minutes=1), energy_min_mwh=0.999999, energy_max_mwh=1.000001
    )
    r = create_field_initializer(tmp_path, clock=lambda: NOW, transport=TimedTransport()).execute(
        InitializeRequest.model_validate(d)
    )
    assert r.payload.errors[0].code == "TIMED_TASK_UNPROVEN", r.payload.errors


def test_optional_task_without_a_timed_proof_is_explicitly_unproven(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_field_initializer
    from oilfield_energy.workflows.field_initialization.contracts import InitializeRequest

    d = closed_loop_document(timing_document())
    d["payload"]["planning_inputs"]["energy_task"].update(
        deadline_at=NOW + timedelta(minutes=1),
        energy_min_mwh=0.999999,
        energy_max_mwh=1.000001,
        required=False,
    )
    r = create_field_initializer(tmp_path, clock=lambda: NOW, transport=TimedTransport()).execute(
        InitializeRequest.model_validate(d)
    )
    assert r.payload.outcome == "accepted", r.payload.errors
    assert r.payload.data.energy_task_guarantee == "unproven"


@pytest.mark.parametrize(
    "change", ["window", "settling", "prepare", "feedback", "unknown", "clock", "precision"]
)
def test_impossible_time_budgets_are_invalid_messages(change):
    from pydantic import ValidationError

    from oilfield_energy.workflows.field_initialization.contracts import InitializeRequest

    d = timing_document()
    p = d["payload"]["control_policy"]
    t = p["bounded_timing"]
    if change == "window":
        t["apply_not_after_seconds"] = 9.0
    elif change == "settling":
        t["resources"][0]["settling_max_seconds"] = 50.0
    elif change == "prepare":
        t["prepare_max_seconds"] = 9.0
    elif change == "feedback":
        p["feedback_timeout_seconds"] = 6.0
    elif change == "clock":
        t["clock_error_bound_seconds"] = 0.001
    elif change == "precision":
        t["apply_not_before_seconds"] = 10.0000004
    else:
        t["allow_late_commands"] = True
    with pytest.raises(ValidationError):
        InitializeRequest.model_validate(d)


def test_timed_recovery_can_resume_after_sampling_within_the_prepare_window(tmp_path):
    from oilfield_energy.bootstrap.field_initialization import create_recovery_services
    from oilfield_energy.workflows.field_initialization.contracts import (
        PrepareResynchronizationRequest,
        ResumeRunRequest,
    )
    from tests.support.field_recovery import SoftwareRecoveryVerifier

    h = TimedHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    h.stop()
    prepare, resume = create_recovery_services(
        tmp_path, clock=lambda: h.now, transport=h.transport, verifier=SoftwareRecoveryVerifier()
    )
    authority = dict(h.document["payload"]["authority"])
    authority["epoch"] += 1
    batch = h.transport.sent[-1]
    request = PrepareResynchronizationRequest.model_validate(
        h.envelope(
            "PrepareResynchronization",
            dict(
                context_id=h.initial.payload.data.context_id,
                resynchronization_id="timed-recovery",
                pending_batch_id=batch.evidence.command_batch_id,
                snapshot=h.snapshot_document["payload"],
                planning_inputs=h.document["payload"]["planning_inputs"],
                authority=authority,
                quiescence=dict(
                    command_batch_id=batch.evidence.command_batch_id,
                    previous_authority=batch.evidence.request.payload.authority,
                    fenced_at=NOW + timedelta(minutes=1),
                    source_ref="analytic-supervisor:fence",
                    settlements=[
                        dict(
                            command_id=r.command_id,
                            resource_id=r.resource_id,
                            disposition="applied",
                            terminal_at=r.applied_at,
                            source_ref="analytic:terminal",
                        )
                        for r in h.transport.results
                    ],
                ),
                reason="operator settled",
            ),
        )
    )
    candidate = prepare.execute(request)
    assert candidate.payload.outcome == "pending", candidate.payload.errors
    h.now += timedelta(seconds=1)
    r = resume.execute(
        ResumeRunRequest.model_validate(
            h.envelope(
                "ResumeRun",
                dict(
                    context_id=request.payload.context_id,
                    resynchronization_id="timed-recovery",
                    proposed_context_id=candidate.payload.data.proposed_context_id,
                    authority=authority,
                ),
            )
        )
    )
    assert r.payload.data.run_state == "ready", r.payload.errors
    h.initial = h.initial.model_copy(
        update={
            "payload": h.initial.payload.model_copy(
                update={
                    "data": h.initial.payload.data.model_copy(
                        update={"context_id": r.payload.data.context_id}
                    )
                }
            )
        }
    )
    h.document["payload"]["authority"] = authority
    assert h.prepare().payload.outcome == "accepted"


def test_compute_crossing_preparation_deadline_withholds_batch(tmp_path, monkeypatch):
    import oilfield_energy.workflows.field_initialization.cycle_preparation as cycles

    h = TimedHarness(tmp_path)
    original = cycles.certify_timed_continuation

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        h.now += timedelta(seconds=6)
        return result

    monkeypatch.setattr(cycles, "certify_timed_continuation", slow)
    r = h.prepare()
    assert r.payload.outcome == "rejected" and not r.payload.data.commands
    assert not h.transport.sent


def test_slow_send_is_unknown_and_reconciled_without_resend(tmp_path, monkeypatch):
    h = TimedHarness(tmp_path)
    h.prepare()
    original = h.transport.send

    def slow(*args):
        result = original(*args)
        h.now += timedelta(seconds=3)
        return result

    monkeypatch.setattr(h.transport, "send", slow)
    request = h.dispatch_request()
    h.services[3].execute(request)
    assert h.query().payload.data.run_state == "resync_required"
    h.services[3].execute(request)
    h.query(reconcile=True)
    assert len(h.transport.sent) == 1


def test_confirmed_task_completion_does_not_stop_later_timed_cycles(tmp_path):
    d = timing_document()
    d["payload"]["planning_inputs"]["energy_task"]["deadline_at"] = NOW + timedelta(minutes=1)
    h = TimedHarness(tmp_path, d)
    for _ in range(3):
        assert h.prepare().payload.outcome == "accepted"
        h.services[3].execute(h.dispatch_request())
        h.observe()
        r = h.report()
        assert r.payload.data.cycle_status == "confirmed", r.payload.errors
        assert r.payload.data.energy_task_outcome == "met"


def test_final_authority_check_cannot_cross_the_preparation_window(tmp_path, monkeypatch):
    h = TimedHarness(tmp_path)
    original = h.transport.authorized
    calls = 0

    def delayed(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            h.now += timedelta(seconds=6)
        return original(*args)

    monkeypatch.setattr(h.transport, "authorized", delayed)
    r = h.prepare()
    assert r.payload.errors[0].code == "EXECUTION_TIMING_UNSUPPORTED"
    assert not r.payload.data.commands


def test_partial_asynchronous_rejection_keeps_the_old_confirmed_boundary(tmp_path):
    h = TimedHarness(tmp_path)
    h.prepare()
    h.services[3].execute(h.dispatch_request())
    h.observe()
    results = list(h.transport.results)
    results[0] = results[0].model_copy(update={"device_status": "rejected", "applied_at": None})
    r = h.report(results=results)
    assert r.payload.errors[0].code == "PARTIAL_OR_UNKNOWN_EXECUTION"
    assert r.payload.data.last_confirmed_cycle_id is None
    assert r.payload.data.confirmed_state.energy_mwh == 1.0
    assert h.query().payload.data.run_state == "resync_required"

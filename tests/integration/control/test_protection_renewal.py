"""Real controller decisions; no plant execution or PCC outcome is inferred."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from oilfield_energy.control_contracts import PccSafetyConstraint
from oilfield_energy.execution_coordinator import ShanchengSafetyCoordinator
from oilfield_energy.modules.control.contracts import ProtectionTargetRecord
from oilfield_energy.shancheng_control import (
    ChannelActuationFeedback,
    CommandDisposition,
    DeviceStatus,
    calculate_shancheng_capabilities,
)
from tests.support.protection import protection_telemetry


def snapshot(minute=0, *, limited=False, fixed=False, **kwargs):
    t = protection_telemetry(**kwargs)
    t = replace(
        t,
        snapshot_id=f"protection-{minute}",
        elapsed_minutes=float(minute),
        observed_at_utc=t.observed_at_utc + timedelta(minutes=minute),
    )
    if limited:
        t = replace(
            t,
            wind_turbines=tuple(
                replace(w, active_power_mw=1, available_active_power_mw=1)
                for w in t.wind_turbines
            ),
            storage=replace(t.storage, charge_power_max_mw=0, discharge_power_max_mw=0),
        )
    if fixed:
        t = replace(
            t,
            wind_turbines=tuple(
                replace(w, status=DeviceStatus(remote_enabled=False))
                for w in t.wind_turbines
            ),
        )
    return t


def allocate(c, t, deficit=0.5, message="allocate"):
    return c.allocate(
        t,
        PccSafetyConstraint("hard", 0.15, message),
        pcc_after_confirmed_pv_mw=0.15 - deficit,
        confirmed_pv_reduction_mw=0,
    )


def latched():
    c = ShanchengSafetyCoordinator(source_epoch="protection-regression")
    result, _ = allocate(c, snapshot())
    assert result.command_accepted and c.active_target_cap_mw == 7.5
    return c


def test_renewal_reprojects_7_5_to_1_and_recovery_cannot_relax_the_cap():
    c = latched()
    previous_allocation = c.last_allocation
    t = snapshot(1, limited=True)
    bounds = calculate_shancheng_capabilities(t)
    assert (bounds.minimum_active_target_mw, bounds.maximum_active_target_mw) == (0, 1)
    d = c.enforce(t, decision_id="renew-1")
    assert d.active_disposition is CommandDisposition.ACCEPTED
    assert "P_TARGET_OUT_OF_RANGE" not in {a.code for a in d.alarms}
    assert c.active_target_cap_mw == d.active_target_mw == 1
    row = c.last_target_record
    assert row.previous_cap_mw == row.required_cap_mw == 7.5
    assert row.submitted_target_mw == row.retained_cap_mw == 1
    assert row.command_accepted is True
    assert row.decision_id == d.command_id == "renew-1"
    assert row.command_sequence == 2
    assert row.command_source_epoch == "protection-regression"
    assert row.command_valid_until_utc == t.observed_at_utc + timedelta(seconds=60)
    assert row.projection.capability_shortfall_mw == 0
    assert ProtectionTargetRecord.model_validate_json(row.model_dump_json()) == row
    assert c.last_allocation is previous_allocation
    d = c.enforce(snapshot(2), decision_id="renew-restored")
    assert d.active_disposition is CommandDisposition.ACCEPTED
    assert c.active_target_cap_mw == d.active_target_mw == 1


def test_minimum_legal_command_preserves_unattainable_cap_until_capability_recovers():
    c = latched()
    c.enforce(snapshot(1, limited=True), decision_id="tighten")
    d = c.enforce(snapshot(2, fixed=True), decision_id="insufficient")
    assert d.active_disposition is CommandDisposition.ACCEPTED
    assert d.active_target_mw == 5.5 and c.active_target_cap_mw == 1
    row = c.last_target_record
    assert row.projection.capability_shortfall_mw == 4.5
    assert row.reason == "minimum_legal_target_with_capability_shortfall"
    d = c.enforce(snapshot(3), decision_id="recovered")
    assert d.active_disposition is CommandDisposition.ACCEPTED
    assert d.active_target_mw == c.active_target_cap_mw == 1
    assert c.last_target_record.projection.capability_shortfall_mw == 0


def test_fresh_allocation_can_accept_a_legal_target_and_still_have_3_5_mw_unserved():
    c = ShanchengSafetyCoordinator(source_epoch="shortfall")
    result, d = allocate(c, snapshot(fixed=True), deficit=6)
    assert (
        result.command_accepted and d.active_disposition is CommandDisposition.ACCEPTED
    )
    assert result.wind_storage_target_mw == 5.5
    assert result.allocated_wind_storage_reduction_mw == 2.5
    assert result.unserved_reduction_mw == 3.5 and not result.fully_allocated
    assert c.active_target_cap_mw == 2  # Requirement is 8 - 6, not the accepted 5.5.
    assert c.last_target_record.projection.capability_shortfall_mw == 3.5


@pytest.mark.parametrize("action", ["renewal", "allocation"])
def test_invalid_measurements_do_not_issue_a_command_or_erase_the_requirement(action):
    c = latched()
    with patch.object(
        c.command_factory,
        "make",
        side_effect=AssertionError("invalid telemetry cannot produce a command"),
    ):
        t = snapshot(1, active_valid=False)
        if action == "renewal":
            d = c.enforce(t, decision_id="invalid")
        else:
            result, d = allocate(c, t, message="invalid")
            assert (
                not result.command_accepted
                and result.unserved_reduction_mw == pytest.approx(0.5)
            )
    assert d.active_disposition is CommandDisposition.BLOCKED
    assert c.active_target_cap_mw == 7.5
    assert c.last_target_record.projection is None
    assert c.last_target_record.submitted_target_mw is None
    assert c.last_target_record.command_accepted is None


def test_unknown_actuation_cannot_tighten_the_cap_as_if_the_command_were_accepted():
    c = latched()
    t = replace(
        snapshot(1, limited=True),
        active_actuation=ChannelActuationFeedback(execution_known=False),
    )
    d = c.enforce(t, decision_id="unknown-execution")
    assert d.active_disposition is CommandDisposition.BLOCKED
    assert c.active_target_cap_mw == 7.5
    assert c.last_target_record.submitted_target_mw == 1
    assert c.last_target_record.command_accepted is False


def test_reallocation_reports_retained_cap_shortfall_even_if_new_pcc_deficit_is_allocated():
    c = latched()
    c.enforce(snapshot(1, limited=True), decision_id="tighten")
    result, _ = allocate(c, snapshot(2, fixed=True), deficit=0.5, message="new-pcc")
    assert result.command_accepted and result.fully_allocated
    assert c.active_target_cap_mw == 1
    assert result.wind_storage_target_mw == 5.5
    assert c.last_target_record.projection.capability_shortfall_mw == 4.5
    assert result.reason == "wind_storage_target_accepted_with_retained_cap_shortfall"


def test_valid_measurements_after_block_do_not_resume_without_explicit_renewal():
    c = latched()
    c.enforce(snapshot(1, active_valid=False), decision_id="invalid")
    with patch.object(
        c.command_factory,
        "make",
        side_effect=AssertionError("recovery observation cannot issue commands"),
    ):
        d = c.track(snapshot(2, limited=True))
    assert d.active_disposition is CommandDisposition.BLOCKED
    assert c.active_target_cap_mw == 7.5
    assert c.last_target_record.command_accepted is None
    d = c.enforce(snapshot(3, limited=True), decision_id="explicit-renewal")
    assert d.active_disposition is CommandDisposition.ACCEPTED
    assert c.active_target_cap_mw == 1


def test_rejected_out_of_order_observation_does_not_replace_previous_evidence():
    c = latched()
    c.enforce(snapshot(2, limited=True), decision_id="later")
    before_record, before_allocation = c.last_target_record, c.last_allocation
    with pytest.raises(ValueError, match="monotonic"):
        allocate(c, snapshot(1), deficit=0)
    assert c.last_target_record is before_record
    assert c.last_allocation is before_allocation
    assert c.active_target_cap_mw == 1


def test_observation_never_renews_or_resumes_a_target_and_release_clears_owner_state():
    c = latched()
    with patch.object(
        c.command_factory,
        "make",
        side_effect=AssertionError("observation is not renewal"),
    ):
        d = c.track(snapshot(1, limited=True))
    assert d.active_disposition is not CommandDisposition.ACCEPTED
    assert c.active_target_cap_mw == 7.5
    assert c.last_target_record.action == "observation"
    assert c.last_target_record.command_accepted is None
    c.release()
    assert not c.active and c.last_allocation is None and c.last_target_record is None
    with patch.object(
        c.controller, "step", side_effect=AssertionError("released owner cannot write")
    ):
        assert c.enforce(snapshot(2), decision_id="released") is None
        assert c.track(snapshot(2)) is None


@pytest.mark.parametrize("active", [False, True])
def test_satisfied_pcc_snapshot_does_not_claim_a_new_command_was_accepted(active):
    c = latched() if active else ShanchengSafetyCoordinator(source_epoch="no-action")
    with patch.object(
        c.command_factory, "make", side_effect=AssertionError("no new command required")
    ):
        result, _ = allocate(c, snapshot(1), deficit=0)
    assert not result.command_accepted and result.wind_storage_target_mw is None
    assert result.fully_allocated  # No measured PCC deficit in THIS snapshot.
    assert result.reason == "pcc_floor_currently_satisfied_no_new_command"


def test_retained_reactive_request_can_reject_legal_active_projection_without_losing_safety_intent():
    from tests.test_shancheng_control import _telemetry

    c = ShanchengSafetyCoordinator(source_epoch="joint-pq")
    t = _telemetry(
        wind_p=(5, 5),
        wind_available=(5, 5),
        svg_q=0,
        svg_status=DeviceStatus(remote_enabled=False),
    )
    q = c.command_factory.make(
        now_utc=t.observed_at_utc, message_id="q-first", reactive_target_mvar=2
    )
    assert c.controller.step(t, q).reactive_disposition is CommandDisposition.ACCEPTED
    result, d = allocate(c, t, deficit=8, message="p-conflict")
    assert (
        not result.command_accepted and result.allocated_wind_storage_reduction_mw == 0
    )
    assert result.unserved_reduction_mw == 8
    assert c.active_target_cap_mw == 2  # Pending safety intent, not accepted output.
    assert d.reactive_target_mvar == 2
    assert "P_CONFLICTS_WITH_RETAINED_Q" in {a.code for a in d.alarms}
    d = c.enforce(
        replace(
            t,
            snapshot_id="joint-next",
            elapsed_minutes=1,
            observed_at_utc=t.observed_at_utc + timedelta(minutes=1),
        ),
        decision_id="still-conflicts",
    )
    assert d.active_disposition is CommandDisposition.REJECTED
    assert c.last_target_record.command_accepted is False
    assert c.active_target_cap_mw == d.reactive_target_mvar == 2

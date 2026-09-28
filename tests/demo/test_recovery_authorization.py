"""Recovery authorization must be demonstrated by actual device trajectories."""

from datetime import timedelta
from math import hypot

import pytest

from oilfield_energy.bootstrap.adapters.demo_case import DemoNetwork, generate_bundle, load_bundle
from oilfield_energy.modules.demo_simulation.api import Fleet, verify_demo
from oilfield_energy.modules.demo_simulation.contracts import Command


@pytest.fixture
def bundle(tmp_path):
    generate_bundle(tmp_path / "dataset")
    return load_bundle(tmp_path / "dataset/bundle.json")[0]


@pytest.fixture
def fleet(bundle):
    result = Fleet(bundle, DemoNetwork(bundle), "recovery-test")
    for _ in range(5):
        result.step()
    return result


def command(fleet, name, device="SC:wind:SC_WT1", p=2.0, q=0.0, seconds=600):
    return Command(
        command_id=name,
        epoch=fleet.epoch,
        device_id=device,
        p_mw=p,
        q_mvar=q,
        expires_at=fleet.frame.simulated_at + timedelta(seconds=seconds),
    )


def actual(fleet, device="SC:wind:SC_WT1"):
    return next(r for r in fleet.readings if r.device_id == device)


def recover(fleet):
    for fault in ("bad_quality", "wind_trip", "bad_quality", "normal"):
        fleet.fault = fault
        fleet.step()
    assert fleet.safe_cycles == 1
    assert not fleet.rearm()
    fleet.step()
    assert fleet.safe_cycles == 2
    assert actual(fleet).p_mw == 0.0
    assert fleet.rearm()


@pytest.mark.parametrize("prior_ticks", [None, 0, 1, 3])
def test_rearm_never_restores_default_or_historical_targets(fleet, prior_ticks):
    if prior_ticks is not None:
        fleet.submit(command(fleet, "old"))
        for _ in range(prior_ticks):
            fleet.step()
    recover(fleet)
    for _ in range(5):
        frame = fleet.step()
        assert all(r.p_mw == 0.0 for r in frame.devices if r.kind == "wind")
        assert not frame.recovery_blocked
    fresh = command(fleet, "fresh", q=0.4)
    assert fleet.submit(fresh).status == "accepted"
    for expected in (1.0, 2.0, 2.0):
        frame = fleet.step()
        assert actual(fleet).p_mw == pytest.approx(expected)
        assert actual(fleet).q_mvar == pytest.approx(min(0.4, 0.30 * expected))
        assert all(
            r.p_mw == 0.0 for r in frame.devices if r.kind == "wind" and r.device_id != "SC:wind:SC_WT1"
        )
    assert fleet.receipts["fresh"].status == "executed"


def test_replay_rejection_and_other_device_commands_do_not_release_hold(fleet):
    old = command(fleet, "old")
    fleet.submit(old)
    for _ in range(3):
        fleet.step()
    recover(fleet)
    assert fleet.submit(old).status == "executed"
    assert fleet.submit(command(fleet, "over-capacity", p=6.0)).status == "rejected"
    assert fleet.submit(command(fleet, "expired", seconds=-1)).reason == "EXPIRED"
    stale = command(fleet, "stale").model_copy(update={"epoch": "earlier-epoch"})
    assert fleet.submit(stale).reason == "EPOCH_MISMATCH"
    with pytest.raises(ValueError, match="conflicts"):
        fleet.submit(old.model_copy(update={"p_mw": 1.0}))
    assert fleet.submit(command(fleet, "storage", "SC:storage:SC_FLEX", 0.5)).status == "accepted"
    for _ in range(3):
        fleet.step()
        assert actual(fleet).p_mw == 0.0
    assert actual(fleet, "SC:storage:SC_FLEX").p_mw == 0.5


def test_command_accepted_while_blocked_cannot_authorize_later_recovery(fleet):
    fleet.fault = "bad_quality"
    fleet.step()
    reduction = command(fleet, "reduction", p=0.0)
    assert fleet.submit(reduction).status == "accepted"
    recover(fleet)
    assert fleet.submit(reduction).status == "executed"
    for _ in range(3):
        fleet.step()
        assert actual(fleet).p_mw == 0.0


def test_released_device_requires_another_fresh_command_after_next_fault(fleet):
    recover(fleet)
    released = command(fleet, "release")
    fleet.submit(released)
    for _ in range(3):
        fleet.step()
    assert actual(fleet).p_mw == 2.0
    recover(fleet)
    assert fleet.submit(released).status == "executed"
    for _ in range(3):
        fleet.step()
        assert actual(fleet).p_mw == 0.0


def test_available_power_can_fall_during_hold_without_automatic_rebound(fleet):
    fleet.fault = "bad_quality"
    fleet.step()
    fleet.fault = "normal"
    fleet.step()
    fleet.step()
    assert fleet.rearm()
    assert 0.0 < actual(fleet).p_mw <= actual(fleet).available_mw
    fleet.fault = "wind_trip"
    fleet.step()
    assert actual(fleet).p_mw == 0.0
    fleet.fault = "normal"
    for _ in range(3):
        fleet.step()
        assert actual(fleet).p_mw == 0.0


def test_expiry_and_ack_timeout_after_rearm_preserve_receipt_meaning(fleet):
    recover(fleet)
    fleet.submit(command(fleet, "expires", seconds=30))
    fleet.step()
    assert fleet.receipts["expires"].status == "expired"
    assert actual(fleet).p_mw == 0.0
    fleet.fault = "ack_timeout"
    fleet.submit(command(fleet, "unknown", p=1.0))
    fleet.step()
    receipt = fleet.receipts["unknown"]
    assert receipt.status == "timeout"
    assert receipt.reason == "ACK_NOT_RECEIVED_EXECUTION_UNKNOWN"
    assert actual(fleet).p_mw == 1.0


def test_rearm_does_not_restore_unexecuted_reactive_target(fleet):
    fleet.submit(command(fleet, "pending-q", q=0.4))
    fleet.fault = "communication_loss"
    fleet.step()
    assert actual(fleet).q_mvar == 0.0
    fleet.fault = "normal"
    fleet.step()
    fleet.step()
    assert fleet.rearm()
    for _ in range(3):
        fleet.step()
        assert actual(fleet).q_mvar == 0.0
    fleet.submit(command(fleet, "fresh-q", q=0.4))
    fleet.step()
    assert actual(fleet).q_mvar == 0.4


@pytest.mark.parametrize("p", [-0.5, 0.5])
def test_new_storage_command_keeps_energy_efficiency_and_capability_limits(fleet, p):
    recover(fleet)
    spec = next(d for d in fleet.bundle.devices if d.device_id == "SC:storage:SC_FLEX")
    initial = actual(fleet, spec.device_id).energy_mwh
    fleet.submit(command(fleet, "storage-energy", spec.device_id, p, q=0.0))
    fleet.step()
    device = actual(fleet, spec.device_id)
    delta = (p / spec.eta_discharge if p > 0 else p * spec.eta_charge) / 60
    assert device.energy_mwh == pytest.approx(initial - delta)
    assert hypot(device.p_mw, device.q_mvar) <= spec.s_max_mva + 1e-9
    assert spec.minimum_mwh <= device.energy_mwh <= spec.energy_mwh


@pytest.mark.parametrize("p", [-2.5, 2.5])
def test_energy_boundary_overrides_recovery_hold(fleet, p):
    recover(fleet)
    fleet.submit(command(fleet, "storage-boundary", "SC:storage:SC_FLEX", p, q=0.0))
    for _ in range(3):
        fleet.step()
    fleet.fault = "bad_quality"
    fleet.step()
    fleet.fault = "normal"
    fleet.step()
    fleet.step()
    assert fleet.rearm()
    spec = next(d for d in fleet.bundle.devices if d.device_id == "SC:storage:SC_FLEX")
    for _ in range(75):
        fleet.step()
        device = actual(fleet, spec.device_id)
        assert spec.minimum_mwh - 1e-9 <= device.energy_mwh <= spec.energy_mwh + 1e-9
        assert hypot(device.p_mw, device.q_mvar) <= spec.s_max_mva + 1e-9
    assert device.energy_mwh == pytest.approx(spec.energy_mwh if p < 0 else spec.minimum_mwh)
    assert device.p_mw == pytest.approx(0.0, abs=1e-9)


def test_fresh_command_does_not_bypass_renewable_availability(fleet):
    recover(fleet)
    assert fleet.submit(command(fleet, "unavailable", p=4.0)).status == "accepted"
    for _ in range(4):
        previous = actual(fleet).p_mw
        fleet.step()
        device = actual(fleet)
        assert device.p_mw <= device.available_mw
        assert device.p_mw <= previous + 1.0 + 1e-9
    assert fleet.receipts["unavailable"].status == "executing"


def test_official_fault_verification_contains_actual_timeout_and_recovery_evidence(bundle):
    # This integration test covers only the device/network part. The full CLI
    # verification separately runs the four real optimization jobs.
    class NoJobs:
        def create(self, request):
            raise AssertionError("device-only verification must not run optimization jobs")

    captured = {}
    report = verify_demo(
        bundle.model_copy(update={"presets": {}}),
        NoJobs(),
        DemoNetwork(bundle),
        save=lambda name, data: captured.__setitem__(name, data),
        clock=lambda: 0.0,
        wait=lambda _: None,
        progress=lambda _: None,
    )
    assert report["passed"]
    assert captured["ack-timeout-accepted.json"]["status"] == "accepted"
    timed_out = captured["telemetry-ack_timeout.json"]
    assert timed_out["receipts"][0]["status"] == "timeout"
    assert next(d for d in timed_out["devices"] if d["device_id"] == "SC:storage:SC_FLEX")["p_mw"] == 0.5
    for case in ("old_command", "no_command"):
        assert report["checks"][f"recovery_hold:{case}"]
        held = captured[f"recovery-{case}-held-2.json"]
        assert all(d["p_mw"] == 0.0 for d in held["devices"] if d["kind"] == "wind")

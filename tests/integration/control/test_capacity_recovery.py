"""Device capacity must independently gate network recovery permission."""

from dataclasses import replace
from pathlib import Path

from oilfield_energy.minute_network import MinuteNetworkEvaluator
from oilfield_energy.power_flow_comparison import read_comparison_request

ROOT = Path(__file__).resolve().parents[3]


def test_device_overcapacity_blocks_recovery_even_when_ac_converges():
    request = read_comparison_request(
        ROOT / "tests/fixtures/field_dataset/fixed_device_comparison.json"
    )
    devices = tuple(
        replace(d, s_max_mva=0.1) if d.resource_type.value == "svg" else d
        for d in request.snapshot.devices
    )
    assert any(d.resource_type.value == "svg" for d in devices)
    snapshot = replace(request.snapshot, devices=devices)
    evaluator = MinuteNetworkEvaluator(
        request.network, request.limits, {d.resource_id: d.bus_id for d in devices}
    )
    feedback = evaluator.evaluate(snapshot, at=snapshot.at, operating_mode_id="normal")
    assert feedback.valid
    assert feedback.device_capacity_within_limits is False
    assert not feedback.recovery_safe
    assert any(v["code"] == "S_MAX" for v in feedback.device_violations)

"""A2: fixed-state AC evidence and one-cycle candidate adoption, no daily study."""

from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest

from oilfield_energy.data import (
    Line,
)
from oilfield_energy.minute_network import MinuteNetworkConfig, MinuteNetworkEvaluator
from oilfield_energy.modules.resources.contracts import WindReactiveCapability
from oilfield_energy.network_model import build_legacy_network_model_v2
from oilfield_energy.network_scenarios import NetworkSecurityLimits
from oilfield_energy.power_flow_comparison import (
    BusLoadState,
    DevicePerturbation,
    DeviceState,
    FixedStateSnapshot,
    device_capability_violations,
    perturb_snapshot,
)
from oilfield_energy.resource_control_contracts import ResourceType

AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def network():
    return build_legacy_network_model_v2(
        network_id="a2-capability-regression",
        buses=["A", "B"],
        pcc_bus_id="A",
        base_mva=10.0,
        lines=[Line("AB", "A", "B", 0.001, 0.001, 6.0)],
    )


def wind(p=2.0, q=0.57, *, identity="wind", curve=None):
    return DeviceState(
        identity,
        "B",
        ResourceType.WIND,
        p,
        q,
        True,
        p_min_mw=0.0,
        p_max_mw=5.0,
        q_min_mvar=-1.5,
        q_max_mvar=1.5,
        s_max_mva=5.5,
        wind_reactive_capability=curve or WindReactiveCapability(5.5, 0.3, 1.5),
    )


def snapshot(*devices):
    return FixedStateSnapshot(
        "a2",
        AT,
        "synthetic A2 fixed-state example",
        "gross_bus_load",
        1.0,
        True,
        True,
        (BusLoadState("A", 0.0, 0.0), BusLoadState("B", 3.5, 0.57)),
        tuple(devices),
    )


def evaluator(devices):
    return MinuteNetworkEvaluator(
        network(),
        NetworkSecurityLimits(0.95, 1.05, 0.15, 4.0, 0.9),
        {d.resource_id: d.bus_id for d in devices},
        config=MinuteNetworkConfig(require_device_capabilities=True),
    )


def test_original_counterexample_improves_pcc_but_fails_device_capability():
    original = snapshot(wind())
    frozen = asdict(original)
    candidate = perturb_snapshot(original, DevicePerturbation("wind", "p_mw", -0.2))
    check = evaluator(original.devices)
    base = check.evaluate(
        original, at=AT, operating_mode_id=network().default_operating_mode_id
    )
    trial = check.evaluate(
        candidate, at=AT, operating_mode_id=network().default_operating_mode_id
    )
    assert base.recovery_safe
    assert base.flow.pcc_import_mw == pytest.approx(1.5002250675202564, abs=1e-10)
    assert trial.flow.pcc_import_mw == pytest.approx(1.7002890982934202, abs=1e-10)
    assert trial.valid and not trial.flow.violations
    assert trial.voltage_within_limits and trial.line_capacity_within_limits
    assert trial.device_capacity_within_limits is False
    assert not trial.recovery_safe
    assert trial.device_violations == (
        {
            "resource_id": "wind",
            "code": "WIND_Q_CAPABILITY",
            "actual": 0.57,
            "limit": pytest.approx(0.54),
        },
    )
    assert candidate.devices[0].q_mvar == 0.57
    assert (
        candidate.devices[0].wind_reactive_capability
        is original.devices[0].wind_reactive_capability
    )
    assert asdict(original) == frozen


@pytest.mark.parametrize(
    "q,violated", [(0.54, False), (-0.54, False), (0.57, True), (-0.57, True)]
)
def test_signed_q_and_exact_ratio_boundary(q, violated):
    result = device_capability_violations(snapshot(wind(1.8, q)), 1e-9)
    assert bool(result) is violated


def test_mva_absolute_and_ratio_limits_intersect_and_check_each_resource():
    # Rising P shrinks converter headroom even when the ratio would permit Q.
    first = wind(4.0, 2.0, curve=WindReactiveCapability(5.0, 1.0, 3.0))
    first = replace(first, q_min_mvar=-3.0, q_max_mvar=3.0, s_max_mva=5.0)
    assert not device_capability_violations(snapshot(first), 1e-9)
    assert any(
        v["code"] == "S_MAX"
        for v in device_capability_violations(snapshot(replace(first, p_mw=4.9)), 1e-9)
    )
    limited = wind(
        2.0, 0.45, identity="absolute", curve=WindReactiveCapability(5.5, 0.3, 0.4)
    )
    other = wind(
        2.0, -0.25, identity="ratio", curve=WindReactiveCapability(5.5, 0.1, 1.5)
    )
    violations = device_capability_violations(snapshot(wind(), limited, other), 1e-9)
    assert {v["resource_id"] for v in violations} == {"absolute", "ratio"}
    assert {v["limit"] for v in violations} == {0.4, 0.2}
    assert (
        violations
        == device_capability_violations(snapshot(other, limited, wind()), 1e-9)[::-1]
    )


@pytest.mark.parametrize(
    "device",
    [replace(wind(), p_max_mw=None), replace(wind(), wind_reactive_capability=None)],
)
def test_missing_capability_is_unknown_and_cannot_authorize_adoption(device):
    result = evaluator((device,)).evaluate(
        snapshot(device), at=AT, operating_mode_id=network().default_operating_mode_id
    )
    assert not result.valid and not result.recovery_safe
    assert result.device_capacity_within_limits is None
    assert result.reasons == ("DEVICE_CAPABILITY_MISSING:wind",)


def test_explicit_converter_only_curve_remains_a_supported_historical_model():
    device = wind(q=0.9, curve=WindReactiveCapability(5.5))
    assert device.capability_complete
    assert not device_capability_violations(snapshot(device), 1e-9)
    assert not device_capability_violations(snapshot(wind(0.0, 0.0)), 1e-9)
    assert device_capability_violations(snapshot(wind(0.0, 0.01)), 1e-9)

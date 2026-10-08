"""Curve-carrying snapshots require v2 and retain authoritative resource values."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from oilfield_energy.modules.resources.contracts import WindReactiveCapability
from oilfield_energy.power_flow_comparison import (
    ComparisonRequest,
    read_comparison_request,
)
from oilfield_energy.resource_control_contracts import ResourceType

ROOT = Path(__file__).resolve().parents[3]


def test_legacy_input_stays_readable_and_does_not_acquire_wind_curves(tmp_path):
    old = read_comparison_request(
        ROOT / "tests/fixtures/field_dataset/fixed_device_comparison.json"
    )
    assert old.schema_version == "fixed-device-comparison-v1"
    assert all(d.wind_reactive_capability is None for d in old.snapshot.devices)
    changed = replace(
        old.snapshot,
        devices=tuple(
            # The old fixture has missing ratings; explicitly declare synthetic
            # values for the NEW v2 example rather than deriving a fictitious bound.
            replace(d, wind_reactive_capability=WindReactiveCapability(5.5, 0.3, 1.5))
            if d.resource_type is ResourceType.WIND
            else d
            for d in old.snapshot.devices
        ),
    )
    with pytest.raises(ValueError, match="require fixed-device-comparison-v2"):
        replace(old, snapshot=changed)
    new = replace(old, schema_version="fixed-device-comparison-v2", snapshot=changed)
    path = tmp_path / "new.json"
    path.write_bytes(TypeAdapter(ComparisonRequest).dump_json(new))
    restored = read_comparison_request(path)
    assert restored == new
    raw = json.loads(path.read_text(encoding="utf-8"))
    for d in raw["snapshot"]["devices"]:
        if d["wind_reactive_capability"] is not None:
            d["wind_reactive_capability"]["typo"] = 1
            break
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        read_comparison_request(path)


def test_comparison_schema_has_no_generation_drift():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/export_comparison_contract.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout

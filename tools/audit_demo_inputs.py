"""Audit synthetic workbook fixtures without importing client asset registries."""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from oilfield_energy.field_data import audit_line_load_workbook, import_short_circuit_workbook


def audit(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["sha256"].items():
        if (
            Path(name).name != name
            or hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest
        ):
            raise ValueError(f"fixture integrity error: {name}")
    short = import_short_circuit_workbook(directory / "short-circuit-synthetic.xlsx")
    normal = audit_line_load_workbook(directory / "line-load-continuous-synthetic.xlsx")
    invalid = audit_line_load_workbook(directory / "line-load-invalid-synthetic.xlsx")
    expected = {
        "TIMESTAMP_INVALID",
        "TIMESTAMP_GAP",
        "TIMESTAMP_DUPLICATE_OR_REVERSED",
        "MEASUREMENT_NONNUMERIC",
        "POWER_FACTOR_OUT_OF_RANGE",
        "NONPOSITIVE_VOLTAGE",
    }
    checks = {
        "three_synthetic_stations": len(short.records) == 3,
        "continuous_24_hour_samples": normal.sheets[0].valid_timestamp_count == 24,
        "normal_only_requires_unit_confirmation": normal.quality.issue_counts
        == {"ENGINEERING_UNIT_MISSING": 5},
        "all_injected_corruption_detected": expected <= set(invalid.quality.issue_counts),
        "no_false_field_readiness": not short.quality.valid
        and not normal.quality.valid
        and not invalid.quality.valid,
    }
    return {
        "synthetic": True,
        "checks": checks,
        "checks_passed": all(checks.values()),
        "notice": "Legacy workbook audit still requires external unit/direction confirmation; these are not approved field inputs.",
        "short_circuit": asdict(short.quality),
        "continuous": asdict(normal.quality),
        "invalid": asdict(invalid.quality),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"Synthetic audit checks passed: {result['checks_passed']}")
    raise SystemExit(0 if result["checks_passed"] else 1)

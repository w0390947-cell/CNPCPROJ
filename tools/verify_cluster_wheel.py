"""Verify installed item04 wheel provenance, contracts and real solver smoke.

Run with the wheel environment's Python -I; dependencies may be reused, but the
project itself must load from that environment's installed wheel, not src.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from zipfile import ZipFile

import oilfield_energy
from oilfield_energy.service import ScenarioType, SimulationRequest, run_simulation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("refuse to replace prior packaging evidence")
    installed = Path(oilfield_energy.__file__).resolve().parent
    assert installed.is_relative_to(Path(sys.prefix).resolve())
    sources = {
        str(p.relative_to(args.source_root / "src")).replace("\\", "/"): p.read_bytes()
        for p in (args.source_root / "src/oilfield_energy").rglob("*.py")
    }
    with ZipFile(args.wheel) as wheel:
        names = {
            n for n in wheel.namelist() if n.startswith("oilfield_energy/") and n.endswith(".py")
        }
        assert names == set(sources)
        for name, content in sources.items():
            assert wheel.read(name) == content, name
            assert (installed.parent / name).read_bytes() == content, name
    result = run_simulation(
        SimulationRequest(
            scenario_type=ScenarioType.CLUSTER_COORDINATION,
            steps=4,
            storage_enabled=False,
        )
    )
    assert result.executive_summary.overall_passed
    assert result.cluster_validation is not None
    assert result.cluster_validation.execution_status == "not_computed"
    assert result.coordination_snapshot is not None
    assert not result.coordination_snapshot.capabilities.storage_enabled
    assert all(
        abs(v) < 1e-8
        for p in result.coordination_snapshot.regions
        for v in (*p.storage_charge_mw, *p.storage_discharge_mw)
    )
    # An old result cannot silently acquire current certification semantics.
    historical = result.model_dump(mode="json")
    historical.pop("cluster_validation")
    historical.pop("coordination_snapshot")
    historical["metadata"]["schema_version"] = "1.1.0"
    for item in historical["validation_items"]:
        item.pop("validation_basis")
    restored = type(result).model_validate(historical)
    assert restored.cluster_validation is None and restored.coordination_snapshot is None
    evidence = {
        "package_path": str(installed),
        "python_isolated_mode": bool(sys.flags.isolated),
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "source_files_matched": len(sources),
        "disabled_storage_service_passed": True,
        "historical_result_preserves_missing_evidence": True,
        "service_result": result.model_dump(mode="json"),
        "dependency_note": "Numerical dependencies reused via dependency-only pth; project loaded from installed wheel.",
    }
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(
        f"Installed wheel: {len(sources)} source files match; disabled-storage solver and historical contract passed."
    )


if __name__ == "__main__":
    main()

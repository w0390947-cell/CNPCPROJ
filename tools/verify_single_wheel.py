"""Check an installed wheel without importing workspace project source."""

import argparse
import hashlib
import json
import subprocess
import sys
from importlib import metadata, util
from pathlib import Path
from zipfile import ZipFile

import oilfield_energy
from oilfield_energy.bootstrap.adapters.cluster_rolling import window_case
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.workflows.single_execution.api import verify_unified_execution


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    installed = Path(oilfield_energy.__file__).resolve().parent
    assert installed.is_relative_to(Path(sys.prefix).resolve())
    assert not any(
        str(args.source_root / "src").lower() == str(p).lower() for p in sys.path
    )
    sources = {
        p.relative_to(args.source_root / "src").as_posix(): p.read_bytes()
        for p in (args.source_root / "src/oilfield_energy").rglob("*.py")
    }
    with ZipFile(args.wheel) as wheel:
        assert not any("archives/" in n or "tests/" in n for n in wheel.namelist())
        assert {
            n
            for n in wheel.namelist()
            if n.startswith("oilfield_energy/") and n.endswith(".py")
        } == set(sources)
        for name, content in sources.items():
            assert wheel.read(name) == content, name
            assert (installed.parent / name).read_bytes() == content, name
    for module in (
        "workflows.single_execution.guarantee",
        "workflows.single_execution.contracts_guarantee",
        "modules.control.application_command_fit",
        "workflows.shancheng_simulation",
        "bootstrap.adapters.simulation_runner",
        "entrypoints.shancheng_simulation",
    ):
        assert util.find_spec("oilfield_energy." + module) is None, module
    scripts = {
        e.name
        for e in metadata.distribution("oilfield-energy-optimization").entry_points
    }
    assert "oilfield-sim" not in scripts and "oilfield-study-inputs" in scripts
    for module in ("oilfield_energy.cli", "oilfield_energy.entrypoints.study_inputs"):
        help_result = subprocess.run(
            [sys.executable, "-I", "-m", module, "--help"],
            capture_output=True,
            check=True,
        )
        assert b"--legacy-milp" not in help_result.stdout
    case = build_synthetic_case(96)
    energy = {m.name: m.storage.e_initial_mwh for m in case.microgrids}
    result = run_simulation(
        SimulationRequest(steps=2), input_case=window_case(case, 0, 2, energy, energy)
    )
    e = result.single_execution
    assert e is not None and e.unified is not None
    assert e.completed_minutes == 30 and e.status == "passed"
    assert e.unified.status == e.unified.tracking_status == "passed"
    verify_unified_execution(e.unified)
    report = {
        "installed_path": str(installed),
        "source_count": len(sources),
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "minutes": e.completed_minutes,
        "safety": e.unified.status,
        "tracking": e.unified.tracking_status,
        "proof_recomputed": True,
        "dependency_note": "Dependency-only site path; editable project hooks not loaded.",
    }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()

"""Archive an installed-package reserve-v2 simulation with reproducible inputs.

No formula, state, trigger flag or solver is replaced. A prior result may supply
only the request; its trajectories and success flags are never reused.
"""

import argparse
import hashlib
import json
import platform
import subprocess
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import oilfield_energy
from oilfield_energy.service import SimulationRequest, run_simulation


def source_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "src/oilfield_energy").rglob("*.py"))
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-result", type=Path)
    args = parser.parse_args()
    root = args.source_root.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    request = SimulationRequest(
        scenario_type="cluster_coordination",
        steps=96,
        solver={"quality_policy": "quality-first-v1"},
    )
    if args.reference_result is not None:
        raw = json.loads(args.reference_result.read_text(encoding="utf-8"))
        request = SimulationRequest.model_validate(raw["request"])
    manifest = source_manifest(root)
    provenance = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "package_path": str(Path(oilfield_energy.__file__).resolve()),
        "python": platform.python_version(),
        "dependencies": {
            name: version(name)
            for name in (
                "numpy",
                "scipy",
                "cvxpy",
                "pyscipopt",
                "pydantic",
                "osqp",
            )
        },
        "git_head": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
        ).strip(),
        "source_sha256": manifest,
        "request": request.model_dump(mode="json"),
        "reference_request_source": str(args.reference_result)
        if args.reference_result
        else None,
        "reference_sha256": hashlib.sha256(
            args.reference_result.read_bytes()
        ).hexdigest()
        if args.reference_result
        else None,
    }
    (args.output / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    def progress(row):
        if row.rolling and row.rolling.phase == "completed":
            if row.rolling.completed_windows % 12 == 0:
                print(f"completed_windows={row.rolling.completed_windows}", flush=True)
        elif "备用不足" in row.label:
            print(row.label, flush=True)

    start = time.monotonic()
    result = run_simulation(request, progress)
    (args.output / "result.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    execution = result.cluster_execution
    assert execution is not None
    summary = {
        "elapsed_seconds": time.monotonic() - start,
        "source_unchanged": source_manifest(root) == manifest,
        "result_schema": result.metadata.schema_version,
        "execution_status": execution.status,
        "validation_total": len(result.validation_items),
        "validation_passed": sum(row.passed for row in result.validation_items),
        "validation_unknown": sum(
            row.assessment_status == "unknown" for row in result.validation_items
        ),
        "validation_failed": [
            row.model_dump(mode="json")
            for row in result.validation_items
            if not row.passed
        ],
        "completed_windows": sum(
            bool(row.actual_end_energy_mwh) for row in execution.rolling_updates
        ),
        "observations": len(execution.minute_reserves),
        "observation_versions": sorted(
            {row.model_version or "historical" for row in execution.minute_reserves}
        ),
        "deficient_observations": sum(
            row.deficient for row in execution.minute_reserves
        ),
        "short_window_attempts": len(execution.reserve_target_adjustments),
        "short_window_adopted": sum(
            row.update.adopted for row in execution.reserve_target_adjustments
        ),
        "minute_points": {
            row.region: len(row.p_mw) for row in execution.stages[2].regions
        },
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    if not summary["source_unchanged"]:
        raise RuntimeError(
            "source changed during audit; result cannot be attributed to one build"
        )


if __name__ == "__main__":
    main()

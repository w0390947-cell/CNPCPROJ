"""Run a captured contiguous unified-network study and archive fresh evidence.

Example: python tools/run_cluster_study.py --scenario normal --output <new-dir>
Use --scenario low_energy for the same network/window at each battery's minimum.
The manifest is the authority for all case deltas; no document is edited.
"""

import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import time

from oilfield_energy.bootstrap.adapters.cluster_study import (
    ClusterStudySpec,
    build_cluster_study,
)
from oilfield_energy.bootstrap.adapters.demo_case import encode
from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.workflows.cluster_execution.api import replay_cluster_safety
from oilfield_energy.workflows.cluster_execution.contracts import ClusterSafetyEvidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=("normal", "low_energy"), default="normal"
    )
    parser.add_argument("--start-minute", type=int, default=0)
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--admm-iterations", type=int, default=1000)
    parser.add_argument("--solver-seconds", type=float, default=60)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    spec = ClusterStudySpec(
        source_start_minute=args.start_minute,
        duration_minutes=args.duration_minutes,
        low_energy=args.scenario == "low_energy",
    )
    case = build_cluster_study(spec)
    request = SimulationRequest(
        name=f"Unified network {args.scenario}",
        scenario_type="cluster_coordination",
        steps=len(case.time_hours),
        admm_max_iterations=args.admm_iterations,
        solver={"time_limit_seconds": args.solver_seconds},
    )
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = encode(case)
    (args.output / "case.json").write_bytes(inputs)
    source = Path(__file__).resolve().parents[1] / "src/oilfield_energy"
    manifest = {
        "spec": asdict(spec),
        "request": request.model_dump(mode="json"),
        "base_dataset_sha256": case.dataset_sha256,
        "case_sha256": sha256(inputs).hexdigest(),
        "input_basis": "captured day-ahead quarter-hour slice; minute input held; final envelope persists",
        "source_sha256": {
            str(p.relative_to(source)): sha256(p.read_bytes()).hexdigest()
            for p in sorted(source.rglob("*.py"))
        },
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    def progress(row):
        if row.rolling is None or row.rolling.phase == "completed":
            print(row.label, flush=True)

    start = time.monotonic()
    result = run_simulation(request, progress, input_case=case)
    (args.output / "result.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    execution = result.cluster_execution
    assert execution is not None
    safety = execution.safety
    print("Replaying completed state and certificate chain", flush=True)
    replayed = (
        replay_cluster_safety(
            ClusterSafetyEvidence.model_validate_json(safety.model_dump_json())
        )
        if safety and safety.initial_continuation
        else 0
    )
    summary = {
        "seconds": time.monotonic() - start,
        "completed_minutes": len(safety.steps) if safety else 0,
        "replayed_minutes": replayed,
        "action_sources": dict(Counter(s.source for s in safety.steps))
        if safety
        else {},
        "windows": [
            {
                "minute": w.start_minute,
                "adopted": w.adopted,
                "reason": w.reason,
                "initial_energy_mwh": w.initial_energy_mwh,
                "actual_end_energy_mwh": w.actual_end_energy_mwh,
            }
            for w in execution.rolling_updates
        ],
        "recovery_tasks": [
            t.model_dump(mode="json") for t in execution.reserve_recovery_history
        ],
        "failure": safety.failure if safety else "missing safety evidence",
    }
    initial_tasks = {
        (t.region, t.direction): t
        for t in execution.reserve_recovery_history
        if t.first_minute == 0
    }
    summary["study_passed"] = (
        (
            replayed == args.duration_minutes
            and any(w.adopted for w in execution.rolling_updates)
            and any(s.source == "economic" for s in safety.steps)
            and (
                not spec.low_energy
                or (
                    len(initial_tasks) == 2 * len(case.microgrids)
                    and all(
                        t.status == "recovered"
                        and t.completed_minute is not None
                        and t.completed_minute <= t.deadline_minute == 15
                        for t in initial_tasks.values()
                    )
                )
            )
        )
        if safety
        else False
    )
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in {"windows", "recovery_tasks"}
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not summary["study_passed"]:
        raise SystemExit(
            "study did not demonstrate execution, economic adoption and required recovery; inspect evidence"
        )


if __name__ == "__main__":
    main()

"""Reproduce full-day single execution and recompute its certificates.

An unsuccessful research metric is reported as such, never converted to a pass.
Use a fresh output directory. The installed package must be importable.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

import oilfield_energy
from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.modules.dispatch.api import pq_witness_violation
from oilfield_energy.workflows.single_execution.api import verify_unified_execution


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--region", default="SC", choices=("SC", "YA_B", "YA_C"))
    parser.add_argument("--reference-steps", type=int, default=8)
    parser.add_argument("--no-storage", action="store_true")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--progress-minutes", type=int, default=60, choices=(15, 60))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    # Record the package actually imported, including isolated wheel runs.
    # Workspace paths alone can describe a different concurrently edited version.
    assert oilfield_energy.__file__ is not None
    package = Path(oilfield_energy.__file__).resolve().parent
    hashes = {
        p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(package.rglob("*.py"))
    }
    (args.output / "source-hashes.json").write_text(
        json.dumps(hashes, indent=2), encoding="utf-8"
    )
    start = perf_counter()

    def progress(row):
        if (
            row.rolling is not None
            and row.rolling.end_minute % args.progress_minutes == 0
        ):
            print(
                f"completed {row.rolling.end_minute} minutes; elapsed {perf_counter() - start:.1f}s",
                flush=True,
            )

    result = run_simulation(
        SimulationRequest(
            region=args.region,
            steps=args.reference_steps,
            storage_enabled=not args.no_storage,
        ),
        progress_callback=progress,
    )
    path = args.output / "result.json"
    path.write_text(result.model_dump_json(), encoding="utf-8")
    e = result.single_execution
    assert e is not None and e.unified is not None
    u = e.unified
    accepted = [
        a
        for a in u.target_admissions
        if a.plan is not None and a.plan.tracking is not None
    ]

    def adjustment(a):
        assert a.plan is not None and a.plan.tracking is not None
        t = a.plan.tracking
        return abs(t.target_p_mw - a.requested_p_mw), abs(
            t.target_q_mvar - a.requested_q_mvar
        )

    assessed = [s for s in u.steps if s.step + 1 >= s.deadline_step]
    checks = {
        c.code: c.model_dump(mode="json")
        for c in result.validation_items
        if c.code.startswith("SINGLE_")
    }
    summary = {
        "imported_package": str(package),
        "python": sys.version,
        "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "execution_seconds": perf_counter() - start,
        "minutes": e.completed_minutes,
        "requested_minutes": e.total_minutes,
        "safety": u.status,
        "tracking": u.tracking_status,
        "hard_task": u.hard_task.model_dump(mode="json"),
        "terminal_energy_mwh": u.steps[-1].after.energy_mwh if u.steps else None,
        "overall_passed": result.executive_summary.overall_passed,
        "commands": dict(Counter(s.disposition for s in u.steps)),
        "filtered_candidates": sum(
            s.disposition == "economic"
            and s.proposed_command is not None
            and s.command != s.proposed_command
            for s in u.steps
        ),
        "allocation_trials": sum(
            len(a.plan.tracking.allocation_trials) for a in accepted
        ),
        "selected_constructions": dict(
            Counter(a.plan.tracking.construction for a in accepted)
        ),
        "multi_resource_feedback_cycles": sum(
            sum(w > 0 for w in p.weights(False)) > 1
            or sum(w > 0 for w in p.weights(True)) > 1
            for a in accepted
            for p in a.plan.tracking.policies
        ),
        "economic_proposals": len(u.economic_proposals),
        "joint_pq_witnesses": sum(
            p.pq_witness is not None for p in u.economic_proposals
        ),
        "max_nominal_pq_residual": max(
            (
                pq_witness_violation(p.pq_witness)
                for p in u.economic_proposals
                if p.pq_witness is not None
            ),
            default=None,
        ),
        "failed_economic_proposals": sum(
            p.command is None for p in u.economic_proposals
        ),
        "target_admission_status": u.target_admission_status,
        "requested_targets": len(u.target_admissions),
        "new_target_requests": sum(a.origin != "retained" for a in u.target_admissions),
        "retained_target_renewals": sum(
            a.origin == "retained" for a in u.target_admissions
        ),
        "accepted_retained_renewals": sum(
            a.origin == "retained" and a.accepted for a in u.target_admissions
        ),
        "accepted_targets": sum(a.accepted for a in u.target_admissions),
        "rejected_targets": sum(not a.accepted for a in u.target_admissions),
        "adjusted_internal_targets": sum(
            a.accepted
            and a.origin == "economic"
            and (a.requested_p_mw, a.requested_q_mvar)
            != (a.plan.tracking.target_p_mw, a.plan.tracking.target_q_mvar)
            for a in u.target_admissions
        ),
        "max_admission_p_adjustment_mw": max(
            (adjustment(a)[0] for a in accepted), default=None
        ),
        "max_admission_q_adjustment_mvar": max(
            (adjustment(a)[1] for a in accepted), default=None
        ),
        "tracking_guarantee_scope": "finite admitted feedback windows; then certified safe continuation",
        "assessed_tracking_cycles": len(assessed),
        "failed_tracking_cycles": sum(not s.tracking_certified for s in assessed),
        "max_assessed_p_error_mw": max(
            (
                max(
                    abs(s.pcc_p_mw.low - s.requested_p_mw),
                    abs(s.pcc_p_mw.high - s.requested_p_mw),
                )
                for s in assessed
            ),
            default=None,
        ),
        "max_assessed_q_error_mvar": max(
            (
                max(
                    abs(s.pcc_q_mvar.low - s.requested_q_mvar),
                    abs(s.pcc_q_mvar.high - s.requested_q_mvar),
                )
                for s in assessed
            ),
            default=None,
        ),
        "checks": checks,
        "proof_recomputed": False,
    }
    if args.replay:
        print(
            "Recomputing every mathematical certificate and executed transition",
            flush=True,
        )
        verify_unified_execution(u)
        summary["proof_recomputed"] = True
    summary["total_seconds"] = perf_counter() - start
    summary["source_changed_during_run"] = [
        name
        for name, digest in hashes.items()
        if not (package / name).exists()
        or hashlib.sha256((package / name).read_bytes()).hexdigest() != digest
    ]
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

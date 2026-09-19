"""Generate real public-service results for the Item 13 client contract audit."""

import argparse
import json
import time
import traceback
from pathlib import Path

from oilfield_energy.job_manager import SimulationJobManager
from oilfield_energy.service import SimulationRequest, run_simulation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    request = {
        "name": "Item 13 synthetic baseline",
        "scenario_type": "single_microgrid",
        "region": "SC",
        "steps": 8,
        "solver": {"formulation": "misocp", "time_limit_seconds": 60},
    }
    event = {
        "event_id": "audit-pv",
        "event_type": "pv_surge",
        "target": "SC",
        "time_axis": "clock_minute",
        "start": 0,
        "end": 180,
        "magnitude": 1.35,
        "label": "Item 13 synthetic branch",
    }
    summary = {}
    for name, payload in (
        ("baseline", request),
        (
            "legacy_single",
            {**request, "solver": {**request["solver"], "formulation": "legacy_milp"}},
        ),
        (
            "legacy_cluster",
            {
                **request,
                "steps": 4,
                "scenario_type": "cluster_coordination",
                "solver": {**request["solver"], "formulation": "legacy_milp"},
            },
        ),
        ("misocp_branch", {**request, "events": [event]}),
    ):
        validated = SimulationRequest.model_validate(payload)
        try:
            result = run_simulation(validated)
            (args.output / f"{name}.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
            summary[name] = {
                "request_accepted": True,
                "formulation": result.metadata.formulation,
                "overall_passed": result.executive_summary.overall_passed,
                "economic_cost_cny": result.executive_summary.optimized_economic_cost_cny,
                "loss_mwh": result.executive_summary.total_active_loss_mwh,
            }
        except Exception as exc:
            (args.output / f"{name}_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            summary[name] = {
                "request_accepted": True,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
    manager = SimulationJobManager(args.output / "jobs")
    try:
        status = manager.create(
            SimulationRequest.model_validate({**request, "solver": {"formulation": "legacy_milp"}})
        )
        deadline = time.monotonic() + 120
        while status.state.value in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            status = manager.get(status.simulation_id)
        summary["legacy_async_job"] = status.model_dump(mode="json")
    finally:
        manager.close()
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

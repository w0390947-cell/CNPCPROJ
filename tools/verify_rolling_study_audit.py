"""Reproduce item 10 observations through the installed study composition root.

This audit observer does not replace solvers or mutate production code.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import oilfield_energy
from oilfield_energy.bootstrap.shancheng_simulation import (
    create_simulation,
    generate_simulation,
)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def collect(folder: Path) -> dict[str, Any]:
    """Independently compare saved stages; never use study passed as the oracle."""
    root = folder / "results"
    recipe = read(folder / "recipe.json")
    plan = read(root / "executed_interval_plan.json")
    rows = [read(path) for path in sorted((root / "intraday").glob("*.json"))]
    first = rows[0]["result"]["optimization"]
    data = plan if "schema_version" in plan else plan["microgrids"]["SC"]
    tracking = read(root / "minute_normal/tracking.json")
    fixed = read(root / "fixed/000.json")
    adopted_p = [row["result"]["optimization"]["microgrids"]["SC"]["p_grid_mw"][0] for row in rows]
    resources = {r["resource_id"]: r for r in recipe["resources"]}
    declared_svg = next(r for r in resources.values() if r["kind"] == "svg")
    svg_schedule = next(s for s in data["resource_schedules"] if s["resource_type"] == "svg")
    actual_q = tracking["svg_reactive_actual_mvar"]
    carry_errors = [
        abs(
            rows[i]["initial_energy_mwh"]
            - rows[i - 1]["result"]["optimization"]["microgrids"]["SC"]["storage_energy_mwh"][1]
        )
        for i in range(1, len(rows))
    ]
    with (root / "minute_normal/minute_network.jsonl").open(encoding="utf-8") as file:
        network = json.loads(next(file))
    devices = network["inputs"]["snapshot"]["devices"]
    observation = {
        "study_passed": read(root / "study_summary.json")["passed"],
        "checks_count": len(read(root / "study_summary.json")["checks"]),
        "interval_count": recipe["intervals"],
        "minute_count": len(tracking["time_minutes"]),
        "rolling_window_count": len(rows),
        "adopted_pcc_steps": len(data["p_grid_mw"]),
        "cluster_pcc_steps": len(plan.get("cluster", {}).get("total_import_mw", [])),
        "adopted_pcc_matches_each_first_step": data["p_grid_mw"] == adopted_p,
        "cluster_identical_first_window": plan.get("cluster") == first["cluster"],
        "objective_identical_first_window": plan["objective_cny"] == first["objective_cny"],
        "certificate_identical_first_window": all(
            plan.get(k) == first[k]
            for k in ("success", "status", "message", "mip_gap", "model_size")
        ),
        "pcc_prefix_max_mismatch_mw": max(
            (
                abs(a - b)
                for a, b in zip(
                    data["p_grid_mw"],
                    plan.get("cluster", {}).get("total_import_mw", []),
                )
            ),
            default=None,
        ),
        "schema_version": plan.get("schema_version"),
        "origins_match_windows": plan.get("origins")
        == [
            dict(
                interval_index=i,
                window_start_interval=row["start_interval"],
                window_end_interval=row["end_interval"],
                source_slot=0,
            )
            for i, row in enumerate(rows)
        ],
        "all_plan_and_minute_ids_match": sorted(resources)
        == sorted(s["resource_id"] for s in data["resource_schedules"])
        == sorted(d["resource_id"] for d in devices),
        "maximum_planned_energy_carry_error_mwh": max(carry_errors, default=0.0),
        "storage_actual_energy_audit": read(root / "storage_execution_audit.json"),
        "declared_ids": sorted(resources),
        "fixed_ids": sorted(d["resource_id"] for d in fixed["snapshot"]["devices"]),
        "adopted_ids": sorted(s["resource_id"] for s in data["resource_schedules"]),
        "minute_ids": sorted(d["resource_id"] for d in devices),
        "svg_declared_s_max_mva": declared_svg["s_max_mva"],
        "svg_declared_q_max_mvar": declared_svg["q_max_mvar"],
        "svg_adopted_q_max_mvar": max(map(abs, svg_schedule["reactive_power_mvar"])),
        "svg_actual_q_max_mvar": max(map(abs, actual_q)),
        "svg_minutes_above_declared_s_max": sum(
            abs(q) > declared_svg["s_max_mva"] + 1e-7 for q in actual_q
        ),
        "svg_first_network_snapshot": next(d for d in devices if d["resource_type"] == "svg"),
        "fixed_device_violations": fixed["device_violations"],
        "first_minute_at": network["inputs"]["snapshot"]["at"],
    }
    write(folder / "evidence.json", observation)
    return observation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument(
        "--case",
        choices=(
            "baseline",
            "midnight",
            "svg",
            "svg_q",
            "svg_s",
            "resource_ids",
            "full",
        ),
        required=True,
    )
    args = parser.parse_args()
    if args.collect_only:
        print(json.dumps(collect(args.output), allow_nan=False))
        return
    args.output.mkdir(parents=True, exist_ok=False)
    recipe = read(args.recipe)
    if args.case != "full":
        recipe.update(intervals=8, start="2026-09-15T12:00:00+08:00", rolling_horizon_intervals=4)
    if args.case == "midnight":
        recipe["start"] = "2026-09-15T23:30:00+08:00"
    if args.case == "svg":
        for resource in recipe["resources"]:
            if resource["kind"] == "svg":
                resource.update(q_max_mvar=1.5, s_max_mva=1.5)
    if args.case == "svg_q":
        for resource in recipe["resources"]:
            if resource["kind"] == "svg":
                resource["q_max_mvar"] = 1.5
    if args.case == "svg_s":
        for resource in recipe["resources"]:
            if resource["kind"] == "svg":
                resource["s_max_mva"] = 1.5
    if args.case == "resource_ids":
        for index, resource in enumerate(recipe["resources"]):
            resource["resource_id"] = f"study-device-{index + 1}"
    config = args.output / "recipe.json"
    write(config, recipe)
    generate_simulation(config, args.output / "inputs")
    outcome = create_simulation().execute(
        args.output / "inputs/manifest.json", args.output / "results"
    )
    result = args.output / "results"
    observation: dict[str, Any] = {
        "package": str(oilfield_energy.__file__),
        "case": args.case,
        "exit_code": outcome.exit_code,
        "summary": read(result / "study_summary.json"),
    }
    if (result / "failure.json").exists():
        observation["failure"] = read(result / "failure.json")
    plan_path = result / "executed_interval_plan.json"
    if plan_path.exists():
        plan = read(plan_path)
        observation["joined_plan"] = {
            "pcc_length": len(
                (plan if "schema_version" in plan else plan["microgrids"]["SC"])["p_grid_mw"]
            ),
            "cluster": plan.get("cluster"),
            "objective_cny": plan["objective_cny"],
            "mip_gap": plan.get("mip_gap"),
            "microgrid_keys": sorted(
                plan if "schema_version" in plan else plan["microgrids"]["SC"]
            ),
        }
    write(args.output / "observation.json", observation)
    if plan_path.exists():
        collect(args.output)
    print(json.dumps(observation, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()

"""Audit item07: time-grid and ordinary storage-response evidence; no source edits."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.data import build_synthetic_case
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.hierarchy_types import TimeScaleConfig
from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs
from oilfield_energy.resource_control_contracts import ResourceType


def ramp_inputs(case, microgrid, result):
    """A declared synthetic target step, not an optimizer feasibility certificate."""
    count = len(case.time_hours)
    points = count * 15
    schedule = np.zeros(count)
    schedule[2:] = 1.0
    data = dict(result.microgrids[microgrid.name])
    data["p_grid_mw"] = np.full(count, 2.0)
    data["q_grid_mvar"] = np.zeros(count)
    data["wind_available_mw"] = np.zeros(count)
    data["resource_schedules"] = tuple(
        replace(
            item,
            active_power_mw=schedule.copy()
            if item.resource_type is ResourceType.STORAGE
            else np.zeros(count),
            reactive_power_mvar=np.zeros(count),
        )
        for item in data["resource_schedules"]
    )
    result = replace(
        result, microgrids={microgrid.name: data}, message="synthetic target-step fixture"
    )
    load = np.full(points, 2.0)
    load[30:] = 4.0

    def series(buses, load_bus=False):
        return tuple(
            BusSeries(
                bus_id=bus,
                values=tuple(
                    float(v)
                    for v in (
                        load if load_bus and bus == microgrid.storage.bus else np.zeros(points)
                    )
                ),
            )
            for bus in buses
        )

    plant = PlantInputs(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        step_minutes=1,
        load_p=series(microgrid.buses, True),
        load_q=series(microgrid.buses),
        wind_available=series(microgrid.wind_available_mw),
        pv_available=series(microgrid.pv_available_mw),
    )
    return result, plant


def save_evidence(args, evidence):
    (args.output / "evidence.json").write_text(
        json.dumps(evidence, indent=2, allow_nan=False), encoding="utf-8"
    )
    root = args.source_root.resolve()
    manifest = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "src/oilfield_energy").rglob("*.py"))
    }
    prior = json.loads((root / args.baseline_manifest).read_text(encoding="utf-8"))
    (args.output / "integrity.json").write_text(
        json.dumps(
            {"production_source_unchanged": manifest == prior, "source_manifest": manifest},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in evidence.items() if not isinstance(v, list)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=96)
    parser.add_argument("--ramp-probe", action="store_true")
    parser.add_argument("--expect-grid-rejection", action="store_true")
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        default=Path("artifacts/audit_20260917/item06/source_manifest_after_fix.json"),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    case = build_synthetic_case(steps=args.steps)
    if args.ramp_probe:
        case = replace(
            case,
            time_hours=np.arange(args.steps) * 0.25,
            assumptions=replace(case.assumptions, dt_hours=0.25),
        )
    microgrid = case.microgrids[0]
    checked = solve_case_ac_consistent(case, [microgrid.name], time_limit_seconds=60)
    assert checked.passed, checked.stop_reason
    result, plant = checked.optimization, None
    if args.ramp_probe:
        result, plant = ramp_inputs(case, microgrid, result)
    config = TimeScaleConfig()
    try:
        tracking = simulate_device_tracking(
            case,
            microgrid,
            result,
            config=config,
            plant_inputs=plant,
            reverse_flow_probability_15=np.zeros(args.steps),
        )
    except ValueError as error:
        if not args.expect_grid_rejection or "exactly divide" not in str(error):
            raise
        save_evidence(
            args,
            {
                "grid_rejected": True,
                "reason": str(error),
                "planning_steps": args.steps,
                "planning_horizon_minutes": args.steps * case.assumptions.dt_hours * 60,
                "planning_ac_consistent": bool(checked.passed),
            },
        )
        return
    if args.expect_grid_rejection:
        raise AssertionError("unsupported grid was unexpectedly accepted")
    power = tracking.storage_actual_mw
    energy = tracking.storage_energy_mwh
    before = np.r_[microgrid.storage.e_initial_mwh, energy[:-1]]
    expected_energy = (
        before
        + np.where(
            power < 0,
            -power * microgrid.storage.eta_charge,
            -power / microgrid.storage.eta_discharge,
        )
        * config.device_step_minutes
        / 60
    )
    ramp = abs(np.diff(power))
    bound = config.active_power_ramp_mw_per_minute * config.device_step_minutes
    violations = np.flatnonzero(ramp > bound + 1e-8) + 1
    examples = []
    for k in violations:
        examples.append(
            {
                "index": int(k),
                "time_minutes": float(tracking.time_minutes[k]),
                "before_power_mw": float(power[k - 1]),
                "after_power_mw": float(power[k]),
                "actual_delta_mw": float(ramp[k - 1]),
                "configured_max_delta_mw": bound,
                "before_energy_mwh": float(before[k]),
                "after_energy_mwh": float(energy[k]),
                "hard_wind_storage_command_accepted": bool(
                    tracking.hard_wind_storage_command_accepted[k]
                ),
                "group_hard_override_active": bool(tracking.group_hard_override_active[k]),
                "local_reverse_flow_reduction_mw": float(
                    tracking.local_reverse_flow_reduction_mw[k]
                ),
                "group_action": str(tracking.group_control_action[k]),
                "soc_flag": bool(tracking.storage_soc_within_limits[k]),
            }
        )
    ordinary = [
        e
        for e in examples
        if not e["hard_wind_storage_command_accepted"]
        and not e["group_hard_override_active"]
        and e["local_reverse_flow_reduction_mw"] == 0
        and e["soc_flag"]
    ]
    effective_interval = len(power) / args.steps * config.device_step_minutes
    evidence = {
        "synthetic_target_step_probe": args.ramp_probe,
        "planning_steps": args.steps,
        "planning_interval_minutes": case.assumptions.dt_hours * 60,
        "planning_horizon_minutes": args.steps * case.assumptions.dt_hours * 60,
        "execution_interval_minutes_per_plan": effective_interval,
        "execution_horizon_minutes": len(power) * config.device_step_minutes,
        "planning_boundary_minutes": (case.time_hours * 60).tolist(),
        "execution_boundary_minutes": (np.arange(args.steps) * effective_interval).tolist(),
        "time_minutes": tracking.time_minutes.tolist(),
        "storage_power_mw": power.tolist(),
        "storage_energy_mwh": energy.tolist(),
        "energy_balance_max_error_mwh": float(np.max(abs(expected_energy - energy))),
        "ramp_violation_count": len(examples),
        "ordinary_ramp_violation_count": len(ordinary),
        "ramp_examples": examples,
        "ordinary_ramp_examples": ordinary,
        "max_storage_step_mw": float(np.max(ramp)),
        "network_security_passed": bool(tracking.network_security_passed),
        "network_invalid_steps": tracking.network_invalid_steps,
        "network_violation_steps": tracking.network_violation_steps,
        "no_reverse_violations": tracking.no_reverse_violations_after_safety,
        "storage_dynamics": [
            dict(asdict(r), ramp_compliant=r.ramp_compliant)
            for r in getattr(tracking, "storage_dynamics", ())
        ],
        "physical_override_steps": sum(
            r.physical_override for r in getattr(tracking, "storage_dynamics", ())
        ),
        "hard_override_steps": sum(
            r.hard_override for r in getattr(tracking, "storage_dynamics", ())
        ),
    }
    save_evidence(args, evidence)
    print(json.dumps({"ordinary_examples": ordinary[:3]}))


if __name__ == "__main__":
    main()

"""Read-only item05 replay against the installed package; outputs synthetic evidence."""

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.group_control import GroupControlSupervisor
from oilfield_energy.hierarchy_types import TimeScaleConfig
from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs
from oilfield_energy.resource_control_contracts import ResourceType


def encode(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def recovery_probe(case, mg, solved, output, pv_time_constant):
    """A slow but operating PV plant; observe unmodified supervisor decisions."""
    steps = len(case.time_hours)
    total = steps * 15
    mg = replace(mg, storage=replace(mg.storage, p_max_mw=1e-6))
    case = replace(case, microgrids=[mg])
    data = dict(solved.microgrids[mg.name])
    data["p_grid_mw"] = np.full(steps, 0.2)
    data["q_grid_mvar"] = np.zeros(steps)
    data["wind_available_mw"] = np.zeros(steps)
    data["resource_schedules"] = tuple(
        replace(
            s,
            active_power_mw=np.full(steps, 2.0 if s.resource_type is ResourceType.PV else 0.0),
            reactive_power_mvar=np.zeros(steps),
        )
        for s in data["resource_schedules"]
    )
    solved = replace(
        solved,
        microgrids={mg.name: data},
        message="audit synthetic control targets, not an optimizer feasibility certificate",
    )
    pv_bus = next(iter(mg.pv_available_mw))

    def series(buses, profile):
        return tuple(BusSeries(bus_id=b, values=tuple(float(v) for v in profile(b))) for b in buses)

    loads = np.full(total, 3.1)
    loads[:30] = 2.2
    plant = PlantInputs(
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        step_minutes=1,
        load_p=series(mg.buses, lambda b: loads if b == pv_bus else np.zeros(total)),
        load_q=series(mg.buses, lambda b: np.zeros(total)),
        wind_available=series(mg.wind_available_mw, lambda b: np.zeros(total)),
        pv_available=series(mg.pv_available_mw, lambda b: np.full(total, 2.0)),
    )
    observed = []
    original = GroupControlSupervisor.step

    def observe(self, snapshot):
        decision = original(self, snapshot)
        observed.append(
            dict(
                time=snapshot.time_minutes,
                measured_pv=snapshot.pv_actual_mw,
                cap_based_curtailment=snapshot.measured_supervisor_curtailment_mw,
                achieved=snapshot.achieved_restoration_mw,
                action=decision.action.value,
                reason=decision.trigger_reason,
                requested=decision.requested_restoration_mw,
                passed=decision.recovery_evaluation_passed,
            )
        )
        return decision

    with patch.object(GroupControlSupervisor, "step", observe):
        result = simulate_device_tracking(
            case,
            mg,
            solved,
            plant_inputs=plant,
            config=TimeScaleConfig(pv_device_time_constant_minutes=pv_time_constant),
            reverse_flow_probability_15=np.ones(steps),
        )
    pairs = []
    pending = None
    for r in observed:
        if r["action"] == "restore":
            pending = r
        if r["passed"] is True and pending is not None:
            actual_change = r["measured_pv"] - pending["measured_pv"]
            pairs.append(
                dict(
                    command_time=pending["time"],
                    evaluation_time=r["time"],
                    requested=pending["requested"],
                    reported_achieved=r["achieved"],
                    actual_pv_change=actual_change,
                    actual_relative_error=abs(actual_change - pending["requested"])
                    / pending["requested"],
                )
            )
            pending = None
    evidence = dict(
        pv_time_constant_minutes=pv_time_constant,
        pairs=pairs,
        observations=observed,
        network_invalid_steps=result.network_invalid_steps,
        network_violation_steps=result.network_violation_steps,
    )
    (output / "slow_recovery.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return {k: v for k, v in evidence.items() if k != "observations"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=96)
    parser.add_argument("--compress-to-quarter-hour", action="store_true")
    parser.add_argument("--recovery", action="store_true")
    parser.add_argument("--pv-time-constant", type=float, default=20.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    case = build_synthetic_case(steps=args.steps)
    if args.compress_to_quarter_hour:
        case = replace(
            case,
            time_hours=np.arange(args.steps) * 0.25,
            assumptions=replace(case.assumptions, dt_hours=0.25),
        )
    mg = case.microgrids[0]
    checked = solve_case_ac_consistent(case, [mg.name], time_limit_seconds=60)
    assert checked.passed
    supervisor_inputs = []
    original_step = GroupControlSupervisor.step

    def record_step(self, snapshot):
        supervisor_inputs.append(snapshot.pcc_power_mw)
        return original_step(self, snapshot)

    with patch.object(GroupControlSupervisor, "step", record_step):
        result = simulate_device_tracking(
            case, mg, checked.optimization, reverse_flow_probability_15=np.zeros(args.steps)
        )
    records = [r for r in result.network_feedback if r["stage"] == "post_control"]
    pre_control_pcc = np.array(
        [r["flow"]["pcc_import_mw"] for r in result.network_feedback if r["stage"] == "pre_control"]
    )
    ac_wind = np.array(
        [
            sum(
                d["p_mw"]
                for d in r["inputs"]["snapshot"]["devices"]
                if d["resource_type"] == "wind"
            )
            for r in records
        ]
    )
    evidence = dict(
        planning_steps=args.steps,
        dt_hours=case.assumptions.dt_hours,
        minute_steps=len(result.time_minutes),
        wind_exceeds_available_count=int(
            np.sum(result.wind_actual_mw > result.wind_available_mw + 1e-8)
        ),
        wind_max_excess_mw=float(np.max(result.wind_actual_mw - result.wind_available_mw)),
        aggregate_vs_ac_wind_max_difference_mw=float(np.max(abs(result.wind_actual_mw - ac_wind))),
        supervisor_vs_pre_control_ac_pcc_max_difference_mw=float(
            np.max(abs(np.asarray(supervisor_inputs) - pre_control_pcc))
        ),
        pv_exceeds_available_count=int(np.sum(result.pv_actual_mw > result.pv_available_mw + 1e-8)),
        pv_max_excess_mw=float(np.max(result.pv_actual_mw - result.pv_available_mw)),
        supervisor_pcc_mw=supervisor_inputs,
        pre_control_ac_pcc_mw=pre_control_pcc.tolist(),
        wind_actual_mw=result.wind_actual_mw.tolist(),
        wind_available_mw=result.wind_available_mw.tolist(),
        ac_wind_mw=ac_wind.tolist(),
        network_security_passed=result.network_security_passed,
        no_reverse_violations=result.no_reverse_violations_after_safety,
        max_difference_sample=records[int(np.argmax(abs(result.wind_actual_mw - ac_wind)))],
    )
    (args.output / "default_minute_evidence.json").write_text(
        json.dumps(evidence, indent=2, default=encode, allow_nan=False), encoding="utf-8"
    )
    manifest = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path("src/oilfield_energy").rglob("*.py")
    }
    (args.output / "source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in evidence.items() if not isinstance(v, (list, dict))}))
    if args.recovery:
        assert case.assumptions.dt_hours == 0.25
        print(
            json.dumps(
                recovery_probe(case, mg, checked.optimization, args.output, args.pv_time_constant)
            )
        )


if __name__ == "__main__":
    main()

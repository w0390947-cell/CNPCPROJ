"""Reproduce item04 findings with the installed package and synthetic inputs.

Observers call the original implementations unchanged. No production files or
input data are modified. Each invocation requires a new evidence directory.
"""

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import patch

import numpy as np

from oilfield_energy import admm, service
from oilfield_energy.data import build_synthetic_case
from oilfield_energy.hierarchical import run_hierarchical_control
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig


def encode(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(type(value).__name__)


def save(output: Path, name: str, value: Any) -> None:
    (output / name).write_text(
        json.dumps(value, default=encode, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def hierarchy(output: Path, steps: int, cap: float | None) -> dict[str, Any]:
    case = build_synthetic_case(steps=steps)
    if cap is not None:
        case = replace(case, cluster_import_limit_mw=cap)
    result = run_hierarchical_control(
        case, admm_config=ADMMConfig(max_iterations=220), time_limit_seconds=60
    )
    names = [mg.name for mg in case.microgrids]
    series = {
        "reference": result.admm.aggregate_import_mw,
        "day_ahead": sum(
            (result.local_milp_results[n].microgrids[n]["p_grid_mw"] for n in names),
            np.zeros(steps),
        ),
        "intraday": sum(
            (result.intraday_milp_results[n].microgrids[n]["p_grid_mw"] for n in names),
            np.zeros(steps),
        ),
        "minute": sum(
            (result.tracking[n].pcc_actual_mw for n in names),
            np.zeros(len(result.tracking[names[0]].time_minutes)),
        ),
    }
    times = result.tracking[names[0]].time_minutes
    assert all(np.array_equal(times, result.tracking[n].time_minutes) for n in names)
    save(output, "aggregate_series.json", series)
    save(output, "comparison.json", result.comparison)
    if result.cluster_validation is not None:
        save(output, "cluster_validation.json", result.cluster_validation)
    save(output, "admm.json", result.admm)
    return {
        "steps": steps,
        "limit_mw": case.cluster_import_limit_mw,
        "comparison": result.comparison,
        "aggregates": {
            name: {
                "peak_mw": float(np.max(values)),
                "violation_steps": int(np.sum(values > case.cluster_import_limit_mw + 1e-5)),
                "peak_index": int(np.argmax(values)),
            }
            for name, values in series.items()
        },
    }


def storage_disabled(output: Path, steps: int) -> dict[str, Any]:
    observed: list[Any] = []
    original = service.run_admm_coordination

    def observe(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        observed.append(result)
        return result

    with patch.object(service, "run_admm_coordination", side_effect=observe):
        result = service.run_simulation(
            service.SimulationRequest(
                scenario_type=service.ScenarioType.CLUSTER_COORDINATION,
                storage_enabled=False,
                steps=steps,
                admm_max_iterations=220,
            )
        )
    save(output, "service_result.json", result.model_dump(mode="json"))
    save(output, "admm.json", observed[0])
    return {
        "storage_enabled": result.request.storage_enabled,
        "overall_passed": result.executive_summary.overall_passed,
        "headline": result.executive_summary.headline,
        "admm_storage": {
            name: {
                "maximum_charge_mw": float(np.max(schedule.storage_charge_mw)),
                "maximum_discharge_mw": float(np.max(schedule.storage_discharge_mw)),
                "throughput_mwh": float(
                    np.sum(schedule.storage_charge_mw + schedule.storage_discharge_mw)
                    * result.metadata.dt_hours
                ),
            }
            for name, schedule in observed[0].schedules.items()
        },
    }


def communication(output: Path, steps: int, seed: int, capture_state: bool) -> dict[str, Any]:
    accepted: dict[str, int] = {}
    rollbacks: list[dict[str, Any]] = []
    original = admm.SimulatedCommunicationChannel.receive
    certified: dict[str, Any] = {}

    def capture(frame: FrameType, event: str, _arg: Any) -> Any:
        # Diagnostic read only: no local variables, arrays, or solver inputs changed.
        if frame.f_code is not admm.run_admm_coordination.__code__:
            return None
        if event == "return":
            for name in ("x_p", "x_q", "z_p", "z_q", "u_p", "u_q"):
                certified[name] = frame.f_locals[name].copy()
            certified["coordination_epoch"] = frame.f_locals["coordination_epoch"]
        return capture

    def observe(channel: Any, receiver: str, iteration: int) -> Any:
        messages = original(channel, receiver, iteration)
        chosen = messages if receiver == "coordinator" else messages[-1:]
        for message in chosen:
            key = f"{message.sender}->{receiver}"
            epoch = (
                message.payload.signal_iteration
                if receiver == "coordinator"
                else message.payload.iteration
            )
            previous = accepted.get(key, -1)
            if epoch < previous:
                rollbacks.append(
                    {"link": key, "iteration": iteration, "previous": previous, "new": epoch}
                )
            accepted[key] = epoch
        return messages

    prior_trace = sys.gettrace()
    try:
        if capture_state:
            sys.settrace(capture)
        with patch.object(admm.SimulatedCommunicationChannel, "receive", new=observe):
            result = admm.run_admm_coordination(
                build_synthetic_case(steps=steps),
                admm_config=ADMMConfig(max_iterations=220),
                communication_config=CommunicationConfig(
                    max_delay_iterations=4, stale_limit_iterations=5, random_seed=seed
                ),
            )
    finally:
        if capture_state:
            sys.settrace(prior_trace)
    save(output, "admm.json", result)
    save(output, "rollbacks.json", rollbacks)
    if capture_state:
        save(output, "convergence_state.json", certified)
        certified_primal = float(
            np.sqrt(
                np.sum((certified["x_p"] - certified["z_p"]) ** 2)
                + np.sum((certified["x_q"] - certified["z_q"]) ** 2)
            )
        )
    else:
        certified_primal = None
    final_primal = float(
        np.sqrt(
            sum(
                np.sum((schedule.p_grid_mw - result.p_references_mw[name]) ** 2)
                + np.sum((schedule.q_grid_mvar - result.q_references_mvar[name]) ** 2)
                for name, schedule in result.schedules.items()
            )
        )
    )
    return {
        "seed": seed,
        "converged": result.converged,
        "coordination_updates": result.coordination_updates,
        "iterations": result.iterations,
        "rollback_count": len(rollbacks),
        "region_rollback_count": sum(r["link"].startswith("coordinator->") for r in rollbacks),
        "final_schedule_epochs": {n: s.signal_iteration for n, s in result.schedules.items()},
        "reported_final_primal": result.history[-1].primal_residual,
        "captured_state_primal": certified_primal,
        "returned_schedule_primal": final_primal,
        "final_primal_tolerance": result.history[-1].primal_tolerance,
        "rejected_stale_messages": result.communication.rejected_stale_messages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hierarchy", "storage", "communication"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--cap", type=float)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--capture-state", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    save(
        args.output,
        "source_manifest.json",
        {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "src/oilfield_energy").rglob("*.py"))
        },
    )
    if args.mode == "hierarchy":
        summary = hierarchy(args.output, args.steps, args.cap)
    elif args.mode == "storage":
        summary = storage_disabled(args.output, args.steps)
    else:
        summary = communication(args.output, args.steps, args.seed, args.capture_state)
    save(args.output, "summary.json", summary)
    print(json.dumps(summary, default=encode, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()

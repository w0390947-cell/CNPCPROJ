"""Replay item03 synthetic economic controls using the installed package.

Writes a new evidence directory; does not modify original audit evidence or input.
"""

import argparse
import json
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.analysis import compare_results
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import ScenarioType, SimulationRequest, run_simulation


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


def full_day(output: Path) -> dict[str, Any]:
    case = build_synthetic_case(steps=96)
    summary: dict[str, Any] = {}
    for label, names in (("single", ["SC"]), ("cluster", [m.name for m in case.microgrids])):
        results = []
        for storage in (False, True):
            checked = solve_case_ac_consistent(
                case,
                names,
                storage_enabled=storage,
                cluster_coordination=len(names) > 1,
                time_limit_seconds=60,
            )
            assert checked.passed, checked.stop_reason
            result = checked.optimization
            results.append(result)
            save(output, f"{label}_{storage}.json", checked)
            summary[f"{label}_{storage}"] = {
                "ac_passed": checked.passed,
                "mip_gap": result.mip_gap,
                "economic_cost_cny": result.cluster["economic_cost_cny"],
            }
        summary[label] = compare_results(*results)
        print(label, summary[label], flush=True)
    return summary


def excess_forecast(output: Path) -> dict[str, Any]:
    case = build_synthetic_case(steps=8)
    original = case.microgrids[0]
    bus = next(iter(original.wind_capacity_mw))
    rows: dict[str, Any] = {}
    for factor in (1.0, 2.0):
        mg = replace(
            original, wind_available_mw={bus: np.full(8, original.wind_capacity_mw[bus] * factor)}
        )
        scenario = replace(case, microgrids=[mg])
        results = []
        for storage in (False, True):
            checked = solve_case_ac_consistent(
                scenario,
                ["SC"],
                storage_enabled=storage,
                cluster_coordination=False,
                time_limit_seconds=60,
                relative_gap=1e-8,
                p_grid_security_floors_mw={"SC": np.full(8, 0.5)},
            )
            assert checked.passed
            results.append(checked.optimization)
            save(output, f"forecast_{factor}_{storage}.json", checked)
        rows[str(factor)] = compare_results(*results)
    for key in ("baseline_economic_cost_cny", "optimized_economic_cost_cny"):
        assert abs(rows["1.0"][key] - rows["2.0"][key]) < 0.02
    assert (
        abs(
            rows["1.0"]["economic_improvement_percent"]
            - rows["2.0"]["economic_improvement_percent"]
        )
        < 1e-4
    )
    print("excess_forecast", rows, flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Refuse stale output reuse: older evidence remains an immutable snapshot.
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {"full_day": full_day(args.output), "excess_forecast": excess_forecast(args.output)}
    service = run_simulation(
        SimulationRequest(
            scenario_type=ScenarioType.CLUSTER_COORDINATION,
            steps=8,
            admm_max_iterations=220,
        )
    )
    assert service.executive_summary.overall_passed
    assert service.reference_economics is not None
    summary["service_reference_economics"] = asdict(service.reference_economics)
    save(args.output, "service_result.json", service.model_dump(mode="json"))
    save(args.output, "summary.json", summary)
    print("service_reference", summary["service_reference_economics"], flush=True)


if __name__ == "__main__":
    main()

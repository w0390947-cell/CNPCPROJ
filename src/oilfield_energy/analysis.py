"""结果汇总、验收指标与结构化导出。"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Sequence

import numpy as np

from .data import ProjectCase
from .model import OptimizationResult


def compare_results(baseline: OptimizationResult, optimized: OptimizationResult) -> Dict[str, float]:
    base_cost = float(baseline.cluster["economic_cost_cny"])
    opt_cost = float(optimized.cluster["economic_cost_cny"])
    improvement = 100.0 * (base_cost - opt_cost) / base_cost if base_cost > 0 else float("nan")
    return {
        "baseline_economic_cost_cny": base_cost,
        "optimized_economic_cost_cny": opt_cost,
        "economic_improvement_percent": improvement,
        "baseline_peak_import_mw": float(baseline.cluster["peak_import_mw"]),
        "optimized_peak_import_mw": float(optimized.cluster["peak_import_mw"]),
    }


def validate_result(
    case: ProjectCase,
    result: OptimizationResult,
    microgrid_names: Sequence[str],
    tolerance: float = 2e-5,
) -> Dict[str, object]:
    lookup = {mg.name: mg for mg in case.microgrids}
    checks: Dict[str, object] = {"solver_success": result.success, "microgrids": {}}
    overall = bool(result.success)
    for name in microgrid_names:
        mg = lookup[name]
        data = result.microgrids[name]
        p_grid = np.asarray(data["p_grid_mw"])
        pf = np.asarray(data["power_factor"])
        voltage = np.asarray(data["voltage_pu"])
        loading = np.asarray(data["line_loading_pu"])
        charge = np.asarray(data["storage_charge_mw"])
        discharge = np.asarray(data["storage_discharge_mw"])
        energy = np.asarray(data["storage_energy_mwh"])
        security_floor = np.asarray(
            data.get(
                "p_grid_security_floor_mw",
                np.full(
                    p_grid.shape,
                    max(mg.p_grid_min_mw, case.assumptions.no_reverse_margin_mw),
                ),
            ),
            dtype=float,
        )
        local = {
            "no_reverse": bool(np.min(p_grid) >= case.assumptions.no_reverse_margin_mw - tolerance),
            "planning_security_floor_compliant": bool(
                np.min(p_grid - security_floor) >= -tolerance
            ),
            "pf_compliant": bool(np.min(pf) >= case.assumptions.pf_min - tolerance),
            "voltage_compliant": bool(
                np.min(voltage) >= mg.voltage_min_pu - tolerance
                and np.max(voltage) <= mg.voltage_max_pu + tolerance
            ),
            "line_capacity_compliant": bool(np.max(loading) <= 1.0 + tolerance),
            "storage_not_simultaneous": bool(np.max(np.minimum(charge, discharge)) <= tolerance),
            "storage_energy_compliant": bool(
                np.min(energy) >= mg.storage.e_min_mwh - tolerance
                and np.max(energy) <= mg.storage.e_max_mwh + tolerance
            ),
            "minimum_pcc_import_mw": float(np.min(p_grid)),
            "minimum_planning_security_headroom_mw": float(
                np.min(p_grid - security_floor)
            ),
            "minimum_power_factor": float(np.min(pf)),
            "minimum_voltage_pu": float(np.min(voltage)),
            "maximum_voltage_pu": float(np.max(voltage)),
            "maximum_line_loading_pu": float(np.max(loading)),
        }
        local["passed"] = all(value for key, value in local.items() if isinstance(value, bool))
        overall = overall and bool(local["passed"])
        checks["microgrids"][name] = local
    if len(microgrid_names) > 1:
        total_import = np.asarray(result.cluster["total_import_mw"])
        checks["cluster_import_limit_compliant"] = bool(
            np.max(total_import) <= case.cluster_import_limit_mw + tolerance
        )
        overall = overall and bool(checks["cluster_import_limit_compliant"])
    checks["passed"] = overall
    return checks


def _json_ready(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def write_summary(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def write_case_data(path: Path, case: ProjectCase) -> None:
    """将程序化算例固化为可审阅、可替换的 JSON 输入快照。"""
    payload = {
        "notice": "SIMULATED RESEARCH DATA; NOT ACTUAL CNPC OR CHANGQING OILFIELD DATA.",
        "time_hours": case.time_hours,
        "price_cny_per_mwh": case.price_cny_per_mwh,
        "assumptions": asdict(case.assumptions),
        "cluster_import_limit_mw": case.cluster_import_limit_mw,
        "microgrids": [asdict(mg) for mg in case.microgrids],
    }
    write_summary(path, payload)


def write_timeseries_csv(
    path: Path,
    case: ProjectCase,
    optimized: OptimizationResult,
    baseline: OptimizationResult,
    microgrid_names: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time_hour", "price_cny_per_mwh", "microgrid",
        "p_grid_optimized_mw", "p_grid_baseline_mw", "q_grid_optimized_mvar",
        "power_factor", "wind_available_mw", "wind_used_mw", "pv_available_mw", "pv_used_mw",
        "storage_charge_mw", "storage_discharge_mw", "storage_energy_mwh",
        "svg_q_mvar", "min_voltage_pu", "max_voltage_pu", "loss_mw",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for name in microgrid_names:
            opt = optimized.microgrids[name]
            base = baseline.microgrids[name]
            voltage = np.asarray(opt["voltage_pu"])
            for t, hour in enumerate(case.time_hours):
                writer.writerow({
                    "time_hour": float(hour),
                    "price_cny_per_mwh": float(case.price_cny_per_mwh[t]),
                    "microgrid": name,
                    "p_grid_optimized_mw": float(opt["p_grid_mw"][t]),
                    "p_grid_baseline_mw": float(base["p_grid_mw"][t]),
                    "q_grid_optimized_mvar": float(opt["q_grid_mvar"][t]),
                    "power_factor": float(opt["power_factor"][t]),
                    "wind_available_mw": float(opt["wind_available_mw"][t]),
                    "wind_used_mw": float(opt["wind_used_mw"][t]),
                    "pv_available_mw": float(opt["pv_available_mw"][t]),
                    "pv_used_mw": float(opt["pv_used_mw"][t]),
                    "storage_charge_mw": float(opt["storage_charge_mw"][t]),
                    "storage_discharge_mw": float(opt["storage_discharge_mw"][t]),
                    "storage_energy_mwh": float(opt["storage_energy_mwh"][t + 1]),
                    "svg_q_mvar": float(opt["svg_q_mvar"][t]),
                    "min_voltage_pu": float(np.min(voltage[:, t])),
                    "max_voltage_pu": float(np.max(voltage[:, t])),
                    "loss_mw": float(opt["loss_mw"][t]),
                })

"""Item08 read-only audit: analytic AC, scenario inputs, and archive identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any, Mapping

import numpy as np

import oilfield_energy
from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkBus,
    NetworkContingency,
    NetworkDataProvenance,
    NetworkModelV2,
    NetworkOperatingMode,
    NetworkSwitch,
    SeriesBranch,
    ShuntCompensator,
    ShuntKind,
    SwitchState,
    assess_network_model,
)
from oilfield_energy.network_scenarios import (
    NetworkOperatingPoint,
    NetworkScenario,
    NetworkScenarioBatch,
    NetworkSecurityLimits,
    build_network_scenarios,
    evaluate_network_scenarios,
    write_network_scenario_outputs,
)


def radial_model(
    *,
    resistance: float = 0.1,
    tap: float = 1.0,
    shunt: float = 0.0,
) -> NetworkModelV2:
    return NetworkModelV2(
        network_id="item08-analytic",
        base_mva=10.0,
        pcc_bus_id="PCC",
        buses=tuple(NetworkBus(b, b, 10.0) for b in ("PCC", "A")),
        branches=(
            SeriesBranch(
                "L",
                "PCC",
                "A",
                NetworkBranchKind.TRANSFORMER,
                resistance,
                0.0,
                200.0,
                fixed_tap_ratio=tap,
            ),
        ),
        switches=(),
        shunts=(ShuntCompensator("C", "A", ShuntKind.CAPACITOR, shunt, 0, 1, 1),) if shunt else (),
        operating_modes=(NetworkOperatingMode("normal", "synthetic radial mode"),),
        default_operating_mode_id="normal",
        contingencies=(NetworkContingency("lose-L", ("L",)),),
        provenance=NetworkDataProvenance("item08 synthetic analytic fixture", "1", synthetic=True),
    )


def point_for(
    model: NetworkModelV2,
    p: Mapping[str, float],
    q: Mapping[str, float] | None = None,
) -> NetworkOperatingPoint:
    return NetworkOperatingPoint(
        "point",
        "item08 synthetic net demand",
        {b.bus_id: p.get(b.bus_id, 0.0) for b in model.buses},
        {b.bus_id: (q or {}).get(b.bus_id, 0.0) for b in model.buses},
        quality_valid=True,
        coherent=True,
    )


def flow_for(model: NetworkModelV2, point: NetworkOperatingPoint) -> dict[str, Any]:
    network = assess_network_model(model).require_current_solver_ready()
    return backward_forward_sweep_resolved(
        network,
        np.array([point.p_demand_mw_by_bus[b.bus_id] for b in network.buses]),
        np.array([point.q_demand_mvar_by_bus[b.bus_id] for b in network.buses]),
    )


def batch_state(batch: NetworkScenarioBatch) -> dict[str, Any]:
    return {
        "inputs_valid": getattr(batch, "inputs_valid", None),
        "all_final_states_secure": batch.all_final_states_secure,
        "n_minus_one_coverage_complete": batch.n_minus_one_coverage_complete,
        "uncovered_n_minus_one": batch.uncovered_n_minus_one,
        "results": [
            {
                "scenario_id": r.scenario.scenario_id,
                "status": r.status.value,
                "stages": [
                    {
                        "topology_status": getattr(s, "topology_status", None),
                        "status": s.status.value,
                        "reasons": s.reasons,
                        "points": [
                            {"status": p.status.value, "reasons": p.reasons} for p in s.points
                        ],
                    }
                    for s in r.stages
                ],
            }
            for r in batch.results
        ],
    }


def collect(output: Path) -> dict[str, Any]:
    limits = NetworkSecurityLimits(0.95, 1.05, 0.0, 200.0, 0.0)
    model = radial_model()
    base = (NetworkScenario("base", "normal"),)
    # Independent real two-bus high-voltage solution: V^2 - E*V + r*Ppu = 0.
    analytic = []
    for tap in (0.98, 1.0, 1.03):
        for p in (0.0, 1.0, 5.0, 10.0, 20.0):
            candidate = radial_model(tap=tap)
            flow = flow_for(candidate, point_for(candidate, {"A": p}))
            expected = (1 / tap + np.sqrt((1 / tap) ** 2 - 4 * 0.1 * p / 10)) / 2
            analytic.append(
                {
                    "tap": tap,
                    "p_mw": p,
                    "converged": bool(flow["converged"]),
                    "voltage_error_pu": float(abs(flow["voltage_pu"][1] - expected)),
                    "balance_error_mw": abs(float(flow["pcc_p_mw"]) - p - float(flow["loss_mw"])),
                }
            )
    # A singular fixed point at V=0 is NOT a valid constant-power load solution.
    collapse_point = point_for(model, {"A": 100.0})
    flow = flow_for(model, collapse_point)
    collapse_batch = evaluate_network_scenarios(model, (collapse_point,), base, limits)
    write_network_scenario_outputs(
        output / "collapse", collapse_batch, model=model, points=(collapse_point,)
    )
    collapse = {
        "demand_mw": 100.0,
        "r_pu": 0.1,
        "base_mva": 10.0,
        "analytic_discriminant": 1.0 - 4 * 0.1 * 100 / 10,
        "converged": bool(flow["converged"]),
        "physical_values_valid": flow.get("physical_values_valid"),
        "stop_reason": flow.get("stop_reason"),
        "iterations": flow["iterations"],
        "voltage_pu": flow["voltage_pu"].tolist(),
        "pcc_mw": flow["pcc_p_mw"],
        "receiving_mw": flow["line_receiving_p_mw"].tolist(),
        "loss_mw": flow["loss_mw"],
        "balance_error_mw": abs(flow["pcc_p_mw"] - 100.0 - flow["loss_mw"]),
        "batch": batch_state(collapse_batch),
    }
    # Invalid snapshots must remain observable even when topology blocks AC.
    valid = point_for(model, {"A": 1.0})
    invalid = replace(valid, quality_valid=False)
    scenarios = build_network_scenarios(model)
    bad_batch = evaluate_network_scenarios(model, (invalid,), scenarios, limits)
    write_network_scenario_outputs(
        output / "invalid_point", bad_batch, model=model, points=(invalid,)
    )
    bad_variants = {
        "quality": invalid,
        "incoherent": replace(valid, coherent=False),
        "nan": replace(valid, p_demand_mw_by_bus={"PCC": 0.0, "A": float("nan")}),
        "missing_bus": replace(valid, p_demand_mw_by_bus={"PCC": 0.0}),
    }
    invalid_variants = {
        name: batch_state(evaluate_network_scenarios(model, (point,), scenarios, limits))
        for name, point in bad_variants.items()
    }
    invalid_fault_only = batch_state(
        evaluate_network_scenarios(
            model,
            (invalid,),
            (NetworkScenario("fault-only", "normal", "lose-L"),),
            limits,
        )
    )
    valid_coverage = batch_state(evaluate_network_scenarios(model, (valid,), scenarios, limits))
    # Export can currently pair old results with different input content under the same IDs.
    original = evaluate_network_scenarios(model, (valid,), base, limits)
    alternate = point_for(model, {"A": 5.0})
    write_network_scenario_outputs(
        output / "mismatched_archive", original, model=model, points=(alternate,)
    )
    replay = evaluate_network_scenarios(model, (alternate,), base, limits)
    mismatch = {
        "original_pcc_mw": original.results[0].stages[0].points[0].pcc_import_mw,
        "archived_input_replayed_pcc_mw": replay.results[0].stages[0].points[0].pcc_import_mw,
        "export_rejected_mismatch": False,
    }
    # Fixed capacitor has Q=Qnom*V^2; P and Q balance include network losses.
    cap_model = radial_model(shunt=0.4, tap=1.03)
    cap = flow_for(cap_model, point_for(cap_model, {"A": 1.0}, {"A": 0.2}))
    shunt = {
        "converged": bool(cap["converged"]),
        "p_balance_error": abs(cap["pcc_p_mw"] - 1.0 - cap["loss_mw"]),
        "q_balance_error": abs(
            cap["pcc_q_mvar"]
            - 0.2
            + cap["fixed_shunt_q_mvar_by_bus"]["A"]
            - cap["reactive_loss_mvar"]
        ),
    }
    # Explicit recovery from an island, preserving the faulted branch exclusion.
    transfer = replace(
        model,
        buses=(*model.buses, NetworkBus("B", "B", 10.0)),
        branches=(
            *model.branches,
            SeriesBranch("LB", "PCC", "B", NetworkBranchKind.LINE, 0.01, 0.02, 4.0),
            SeriesBranch("TIE", "B", "A", NetworkBranchKind.LINE, 0.01, 0.02, 4.0),
        ),
        switches=(NetworkSwitch("ST", "TIE", normally_closed=False),),
        operating_modes=(
            *model.operating_modes,
            NetworkOperatingMode(
                "transfer", "explicit transfer", switch_states=(SwitchState("ST", True),)
            ),
        ),
    )
    transfer_point = point_for(transfer, {"A": 1.0, "B": 0.5})
    transfer_batch = evaluate_network_scenarios(
        transfer,
        (transfer_point,),
        (NetworkScenario("transfer", "normal", "lose-L", "transfer"),),
        limits,
    )
    permuted = replace(
        transfer, buses=tuple(reversed(transfer.buses)), branches=tuple(reversed(transfer.branches))
    )
    other = evaluate_network_scenarios(
        permuted,
        (transfer_point,),
        (NetworkScenario("transfer", "normal", "lose-L", "transfer"),),
        limits,
    )
    final = transfer_batch.results[0].stages[-1]
    transfer_check = {
        "batch": batch_state(transfer_batch),
        "active_branches": final.active_branch_ids,
        "permuted_pcc_error_mw": abs(
            final.points[0].pcc_import_mw - other.results[0].stages[-1].points[0].pcc_import_mw
        ),
    }
    return {
        "analytic_cases": analytic,
        "collapse": collapse,
        "invalid_snapshot": batch_state(bad_batch),
        "invalid_variants": invalid_variants,
        "invalid_fault_only": invalid_fault_only,
        "valid_coverage": valid_coverage,
        "archive_mismatch": mismatch,
        "shunt_balance": shunt,
        "transfer": transfer_check,
        "security_limits": asdict(limits),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        default=Path("artifacts/audit_20260917/item07/source_manifest_after_fix.json"),
    )
    parser.add_argument("--expect-repaired", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = collect(args.output)
    if args.expect_repaired:
        assert not evidence["collapse"]["converged"]
        assert evidence["collapse"]["batch"]["results"][0]["status"] == "not_converged"
        assert all(
            not r["n_minus_one_coverage_complete"] and r["inputs_valid"] is False
            for r in evidence["invalid_variants"].values()
        )
        assert evidence["valid_coverage"]["n_minus_one_coverage_complete"]
        assert evidence["transfer"]["batch"]["all_final_states_secure"]
    (args.output / "evidence.json").write_text(
        json.dumps(evidence, indent=2, allow_nan=False), encoding="utf-8"
    )
    root = args.source_root.resolve()
    manifest = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "src/oilfield_energy").rglob("*.py"))
    }
    baseline = json.loads((root / args.baseline_manifest).read_text(encoding="utf-8"))
    package_root = Path(oilfield_energy.__file__).resolve().parent
    package_matches_source = all(
        hashlib.sha256(
            (package_root / Path(name).relative_to("src/oilfield_energy")).read_bytes()
        ).hexdigest()
        == digest
        for name, digest in manifest.items()
    )
    integrity = {
        "production_source_unchanged": manifest == baseline,
        "source_manifest": manifest,
        "package_root": str(package_root),
        "package_matches_source": package_matches_source,
        "python": sys.version,
        "dependencies": {name: version(name) for name in ("numpy", "scipy", "cvxpy", "pyscipopt")},
        "analytic_cases_passed": all(
            c["converged"] and c["voltage_error_pu"] < 1e-8 and c["balance_error_mw"] < 1e-7
            for c in evidence["analytic_cases"]
        ),
        "dependency_note": "Installed-wheel environment reuses dependencies through a dependency-only pth; not an independent dependency-resolution test.",
    }
    (args.output / "integrity.json").write_text(json.dumps(integrity, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "source_unchanged": manifest == baseline,
                "source_count": len(manifest),
                "collapse": evidence["collapse"],
                "invalid_snapshot": evidence["invalid_snapshot"],
                "archive_mismatch": evidence["archive_mismatch"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

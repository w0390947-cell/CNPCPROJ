"""Item08 independent nodal-admittance reference for synthetic radial networks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import root

from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkBus,
    NetworkDataProvenance,
    NetworkModelV2,
    NetworkOperatingMode,
    SeriesBranch,
    ShuntCompensator,
    ShuntKind,
    assess_network_model,
)


def check_cases() -> dict[str, Any]:
    rng = np.random.default_rng(20260918)
    records = []
    for case_id in range(30):
        ids = tuple(f"B{i}" for i in range(5))
        branches = []
        for child in range(1, 5):
            parent = int(rng.integers(child))
            endpoints = (ids[parent], ids[child])
            if rng.random() < 0.5:
                endpoints = endpoints[::-1]
            branches.append(
                SeriesBranch(
                    f"L{child}",
                    *endpoints,
                    NetworkBranchKind.TRANSFORMER,
                    float(rng.uniform(0.001, 0.03)),
                    float(rng.uniform(0.005, 0.04)),
                    100.0,
                    fixed_tap_ratio=float(rng.uniform(0.97, 1.03)),
                )
            )
        model = NetworkModelV2(
            network_id=f"nodal-{case_id}",
            base_mva=10.0,
            pcc_bus_id=ids[0],
            buses=tuple(NetworkBus(str(b), str(b), 10.0) for b in rng.permutation(ids)),
            branches=tuple(reversed(branches)),
            switches=(),
            shunts=(
                ShuntCompensator(
                    "shunt",
                    ids[4],
                    ShuntKind.CAPACITOR if case_id % 2 else ShuntKind.REACTOR,
                    0.2,
                    0,
                    1,
                    1,
                ),
            ),
            operating_modes=(NetworkOperatingMode("normal", "synthetic reference"),),
            default_operating_mode_id="normal",
            provenance=NetworkDataProvenance(
                "seeded independent nodal reference", "1", synthetic=True
            ),
        )
        index = {b.bus_id: i for i, b in enumerate(model.buses)}
        p = rng.uniform(-0.3, 1.0, 5)
        q = rng.uniform(-0.1, 0.3, 5)
        # Build admittance directly from DECLARED orientation, without the resolver.
        ybus = np.zeros((5, 5), dtype=complex)
        for b in model.branches:
            i, j = index[b.from_bus_id], index[b.to_bus_id]
            y, a = 1 / complex(b.r_pu, b.x_pu), b.fixed_tap_ratio
            ybus[i, i] += y / a**2
            ybus[j, j] += y
            ybus[i, j] -= y / a
            ybus[j, i] -= y / a
        shunt = model.shunts[0]
        sign = 1 if shunt.kind is ShuntKind.CAPACITOR else -1
        ybus[index[shunt.bus_id], index[shunt.bus_id]] += 1j * sign * 0.2 / 10
        unknown = [i for i in range(5) if i != index[model.pcc_bus_id]]

        def voltages(x):
            v = np.ones(5, dtype=complex)
            v[unknown] = x[:4] + 1j * x[4:]
            return v

        def mismatch(x):
            v = voltages(x)
            residual = (v * np.conj(ybus @ v) + (p + 1j * q) / 10)[unknown]
            return np.r_[residual.real, residual.imag]

        solution = root(mismatch, np.r_[np.ones(4), np.zeros(4)], tol=1e-11)
        v = voltages(solution.x)
        k = index[model.pcc_bus_id]
        reference_pcc = v[k] * np.conj((ybus @ v)[k]) * 10 + p[k] + 1j * q[k]
        loss = sum(
            b.r_pu
            * abs(
                (v[index[b.from_bus_id]] / b.fixed_tap_ratio - v[index[b.to_bus_id]])
                / complex(b.r_pu, b.x_pu)
            )
            ** 2
            * 10
            for b in model.branches
        )
        resolved = assess_network_model(model).require_current_solver_ready()
        flow = backward_forward_sweep_resolved(resolved, p, q)
        records.append(
            {
                "case_id": case_id,
                "reference_solver_success": bool(solution.success),
                "reference_residual_pu": float(np.max(np.abs(mismatch(solution.x)))),
                "sweep_converged": bool(flow["converged"]),
                "max_voltage_error_pu": float(np.max(np.abs(np.abs(v) - flow["voltage_pu"]))),
                "pcc_complex_error_mva": float(
                    abs(reference_pcc - complex(flow["pcc_p_mw"], flow["pcc_q_mvar"]))
                ),
                "loss_error_mw": float(abs(loss - flow["loss_mw"])),
            }
        )
    return {
        "seed": 20260918,
        "count": len(records),
        "cases": records,
        "passed": all(
            r["reference_residual_pu"] < 1e-9
            and r["sweep_converged"]
            and r["max_voltage_error_pu"] < 1e-8
            and r["pcc_complex_error_mva"] < 1e-7
            and r["loss_error_mw"] < 1e-7
            for r in records
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = check_cases()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

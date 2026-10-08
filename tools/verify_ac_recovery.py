"""Verify installed AC recovery on an explicit synthetic case, without tests imports."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import oilfield_energy

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.data import (
    Line,
    MicrogridData,
    ModelAssumptions,
    ProjectCase,
    Storage,
)
from oilfield_energy.modules.dispatch.contracts import PCCTrackingLimits
from oilfield_energy.network_model import build_legacy_network_model_v2


def synthetic_case():
    buses = ["pcc", "load", "pv"]
    lines = [
        Line("load_line", "pcc", "load", 0.01, 0.01, 10.0),
        Line("pv_line", "pcc", "pv", 0.01, 0.01, 10.0),
    ]
    network = build_legacy_network_model_v2(
        network_id="A", buses=buses, pcc_bus_id="pcc", base_mva=10.0, lines=lines
    )
    mg = MicrogridData(
        name="A",
        buses=buses,
        lines=lines,
        pcc_bus="pcc",
        base_mva=10.0,
        maximum_load_mw=7.0,
        p_grid_max_mw=10.0,
        p_grid_min_mw=0.15,
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        load_p_mw={b: np.array([7.0 if b == "load" else 0.0]) for b in buses},
        load_q_mvar={b: np.zeros(1) for b in buses},
        wind_available_mw={},
        wind_capacity_mw={},
        wind_capacity_mva={},
        pv_available_mw={"pv": np.zeros(1)},
        pv_capacity_mw={"pv": 4.0},
        pv_capacity_mva={"pv": 4.4},
        storage=Storage("load", 1.0, 4.0, 0.5, 2.0, 0.95, 0.95, 1.2),
        svg_bus="load",
        svg_q_min_mvar=0.0,
        svg_q_max_mvar=0.0,
        pv_reactive_enabled={"pv": False},
        storage_reactive_enabled=False,
        network_model_v2=network,
        network_operating_mode_id=network.default_operating_mode_id,
    )
    return ProjectCase(
        np.array([0.0]), np.array([600.0]), [mg], ModelAssumptions(), 30.0
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    installed = Path(oilfield_energy.__file__).resolve().parent
    assert installed.is_relative_to(Path(sys.prefix).resolve())
    assert not any(
        Path(p).resolve() == (args.source_root / "src").resolve() for p in sys.path
    )
    source_files = {
        p.relative_to(args.source_root / "src").as_posix(): p.read_bytes()
        for p in (args.source_root / "src/oilfield_energy").rglob("*.py")
    }
    with ZipFile(args.wheel) as wheel:
        assert {
            n
            for n in wheel.namelist()
            if n.startswith("oilfield_energy/") and n.endswith(".py")
        } == set(source_files)
        for name, content in source_files.items():
            assert wheel.read(name) == content, name
            assert (installed.parent / name).read_bytes() == content, name
    case = synthetic_case()
    options = dict(
        cluster_coordination=False,
        pcc_targets={"A": {"p_mw": np.array([7.05]), "q_mvar": np.array([0.05])}},
        tracking_limits=PCCTrackingLimits(0.05, 0.05, 1e-6),
        time_limit_seconds=10.0,
    )
    old = solve_case_ac_consistent(case, recovery_policy=None, **options)
    assert not old.passed and old.stop_reason == "ac_consistency_iterations_exhausted"
    recovered = solve_case_ac_consistent(case, **options)
    assert recovered.passed and recovered.recovery is not None
    physical = recovered.ac_validation["microgrids"]["A"]
    assert abs(physical["pcc_p_mw"][0] - 7.05) <= 0.05 + 1e-6
    assert abs(physical["pcc_q_mvar"][0] - 0.05) <= 0.05 + 1e-6
    assert recovered.optimization.cluster["original_constraints_verified"]
    report = {
        "source_files_verified": len(source_files),
        "installed_path": str(installed),
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "input": "explicit synthetic 3-bus case in tools/verify_ac_recovery.py",
        "old_stop_reason": old.stop_reason,
        "new_stop_reason": recovered.stop_reason,
        "physical": physical,
        "recovery": recovered.recovery.model_dump(mode="json"),
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in [
                    "installed_path",
                    "source_files_verified",
                    "wheel_sha256",
                    "old_stop_reason",
                    "new_stop_reason",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()

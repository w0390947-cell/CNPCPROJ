"""Capture a short, current, real single result for transport and UI tests."""

import argparse
from pathlib import Path

from oilfield_energy.bootstrap.adapters.cluster_rolling import window_case
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, run_simulation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    case = build_synthetic_case(96)
    energy = {m.name: m.storage.e_initial_mwh for m in case.microgrids}
    result = run_simulation(
        SimulationRequest(steps=2), input_case=window_case(case, 0, 2, energy, energy)
    )
    assert result.single_execution is not None and result.single_execution.completed_minutes == 30
    args.output.write_text(result.model_dump_json(), encoding="utf-8")


if __name__ == "__main__":
    main()

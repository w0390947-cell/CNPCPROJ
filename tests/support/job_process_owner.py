"""Fault-injection owner process for lifecycle tests; real installed worker."""

import argparse
import json
import os
import time
from pathlib import Path

from oilfield_energy.job_manager import SimulationJobManager
from oilfield_energy.service import SimulationRequest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--solving", action="store_true")
    args = parser.parse_args()
    manager = SimulationJobManager(args.root / "jobs")
    status = manager.create(SimulationRequest(steps=96))
    if args.solving:
        deadline = time.monotonic() + 30
        while status.stage not in {"solving_baseline", "solving_optimized"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("worker did not start solving")
            time.sleep(0.01)
            status = manager.get(status.simulation_id)
    (args.root / "ready.json").write_text(
        json.dumps(
            {
                "simulation_id": status.simulation_id,
                "worker_pid": manager._active_process.pid,
                "attempt": str(manager._active_attempt),
            }
        ),
        encoding="utf-8",
    )
    while not (args.root / "crash").exists():
        time.sleep(0.01)
    os._exit(0)


if __name__ == "__main__":
    main()

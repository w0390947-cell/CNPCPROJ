"""独立仿真工作进程入口。"""

from __future__ import annotations

import argparse
import traceback
from pathlib import Path

from .job_manager import (
    JobState,
    JobStatus,
    atomic_write_json,
    progress_status,
    utc_now,
    write_job_status,
)
from .runtime.worker_lifetime import watch_owner_pipe
from .service import SimulationRequest, run_simulation


def run_session(session_dir: Path, *, managed: bool = False) -> None:
    if managed:
        watch_owner_pipe()
    status_path = session_dir / "status.json"
    request_path = session_dir / "request.json"
    status = JobStatus.model_validate_json(status_path.read_text(encoding="utf-8"))
    try:
        request = SimulationRequest.model_validate_json(request_path.read_text(encoding="utf-8"))

        def report(update) -> None:
            nonlocal status
            status = progress_status(status, update)
            write_job_status(session_dir, status)

        result = run_simulation(request, report)
        atomic_write_json(session_dir / "result.json", result.model_dump(mode="json"))
        status = status.model_copy(update={
            "state": JobState.SUCCEEDED,
            "updated_at": utc_now(),
            "stage": "succeeded",
            "stage_label": "仿真与校核完成",
            "stage_sequence": 6,
            "result_available": True,
            "error_code": None,
            "error_message": None,
        })
        write_job_status(session_dir, status)
    except Exception as exc:
        (session_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        write_job_status(session_dir, status.model_copy(update={
            "state": JobState.FAILED,
            "updated_at": utc_now(),
            "stage": "failed",
            "stage_label": "仿真失败",
            "result_available": False,
            "error_code": "SIMULATION_FAILED",
            "error_message": str(exc),
        }))
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--managed", action="store_true")
    args = parser.parse_args()
    run_session(args.session.resolve(), managed=args.managed)


if __name__ == "__main__":
    main()

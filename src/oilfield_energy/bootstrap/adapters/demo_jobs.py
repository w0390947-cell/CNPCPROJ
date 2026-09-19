"""Compatibility adapter for existing independent solver jobs."""
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

import hashlib
import traceback
from importlib import metadata
from pathlib import Path

import cvxpy as cp
from pydantic import JsonValue

from oilfield_energy.job_manager import (
    JobState,
    JobStatus,
    SimulationJobManager,
    atomic_write_json,
    progress_status,
    utc_now,
    write_job_status,
)
from oilfield_energy.runtime.worker_lifetime import watch_owner_pipe
from oilfield_energy.service import ProgressUpdate, SimulationRequest, run_simulation

from .demo_case import case_from_bundle, load_bundle


class DemoJobs:
    def __init__(self, root: Path, raw: bytes):
        self.manager = SimulationJobManager(
            root, input_bundle=raw, worker_module="oilfield_energy.entrypoints.demo_worker"
        )

    def health(self) -> dict[str, JsonValue]:
        return {
            "status": "ok",
            "service": "oilfield-energy-simulation",
            "schema_version": "1.0.0",
            "solvers": [str(solver) for solver in sorted(cp.installed_solvers())],
            "packages": {
                p: metadata.version(p) for p in ("numpy", "scipy", "cvxpy", "pyscipopt", "fastapi")
            },
        }

    def create(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return self.manager.create(SimulationRequest.model_validate(request)).model_dump(
            mode="json"
        )

    def get(self, job_id: str) -> dict[str, JsonValue]:
        return self.manager.get(job_id).model_dump(mode="json")

    def cancel(self, job_id: str) -> dict[str, JsonValue]:
        return self.manager.cancel(job_id).model_dump(mode="json")

    def result(self, job_id: str) -> dict[str, JsonValue]:
        from oilfield_energy.service import SimulationResult

        return SimulationResult.model_validate_json(self.manager.read_result(job_id)).model_dump(
            mode="json"
        )

    def tick(self) -> None:
        self.manager.tick()

    def close(self) -> None:
        self.manager.close()


def run_demo_session(session: Path, *, managed: bool = False) -> None:
    if managed:
        watch_owner_pipe()
    status = JobStatus.model_validate_json((session / "status.json").read_bytes())
    try:
        bundle, raw = load_bundle(session / "bundle.json")
        request = SimulationRequest.model_validate_json((session / "request.json").read_bytes())
        case = case_from_bundle(bundle, request.steps)
        atomic_write_json(
            session / "input_provenance.json",
            dict(
                dataset_id=bundle.dataset_id,
                synthetic=True,
                sha256=hashlib.sha256(raw).hexdigest(),
                resampling="periodic linear interpolation",
                steps=request.steps,
            ),
        )

        def report(update: ProgressUpdate) -> None:
            nonlocal status
            status = progress_status(status, update)
            write_job_status(session, status)

        result = run_simulation(request, report, input_case=case)
        result = result.model_copy(
            update={
                "metadata": result.metadata.model_copy(
                    update={
                        "data_notice": f"合成演示资料 {bundle.dataset_id}；SHA-256 {hashlib.sha256(raw).hexdigest()}；不代表甲方真实运行数据。"
                    }
                )
            }
        )
        atomic_write_json(session / "result.json", result.model_dump(mode="json"))
        write_job_status(
            session,
            status.model_copy(
                update=dict(
                    state=JobState.SUCCEEDED,
                    updated_at=utc_now(),
                    result_available=True,
                    stage="succeeded",
                    stage_label="模拟计算完成",
                )
            ),
        )
    except Exception as exc:
        (session / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        write_job_status(
            session,
            status.model_copy(
                update=dict(
                    state=JobState.FAILED,
                    updated_at=utc_now(),
                    result_available=False,
                    error_code="SIMULATION_FAILED",
                    error_message=str(exc),
                )
            ),
        )
        raise

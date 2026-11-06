"""统一 FastAPI 网页入口：健康检查、仿真任务与持续模拟设备。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from contextlib import asynccontextmanager, nullcontext
from importlib import metadata
from pathlib import Path
from typing import Annotated, Literal, cast

import cvxpy as cp
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from starlette.requests import HTTPConnection

from .bootstrap.demo import DemoConfig, demo_device_router, live_demo, materialize_default_bundle
from .bootstrap.field_initialization import create_field_initializer, create_planning_updater, create_pcc_target_requester, create_snapshot_submitter, create_control_cycle_preparer, create_execution_services, create_recovery_services
from .workflows.field_initialization.api import ExecutionTransport, RecoveryVerifier
from .entrypoints.field_initialization_http import router as field_initialization_router
from .job_manager import JobStatus, SimulationJobManager
from .runtime.periodic import PeriodicWorker
from .runtime.retention import RetentionCapacityError, RetentionPolicy
from .service import ScenarioType, SimulationRequest, SimulationResult, run_simulation
from .modules.studies.contracts_patent2 import Patent2Inputs, Patent2RunRequest, Patent2Scenario
from .bootstrap.adapters.patent2_example import describe_example
from .bootstrap.adapters.patent2_library import library_path, read_library
from .bootstrap.adapters.single_example import describe_single_example
from .bootstrap.adapters import single_library
from .workflows.single_execution.contracts_example import SingleExampleInputs


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    service: str
    schema_version: str
    solvers: list[str]
    packages: dict[str, str]


router = APIRouter()


def _jobs(connection: HTTPConnection) -> SimulationJobManager:
    pump = cast(PeriodicWorker, connection.app.state.job_pump)
    if pump.error is not None:
        raise HTTPException(status_code=503, detail="simulation queue is unavailable")
    return cast(SimulationJobManager, connection.app.state.job_manager)


Jobs = Annotated[SimulationJobManager, Depends(_jobs)]


def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="oilfield-energy-simulation",
        schema_version="1.0.0",
        solvers=sorted(cp.installed_solvers()),
        packages={
            package: metadata.version(package)
            for package in ("numpy", "scipy", "cvxpy", "pyscipopt", "fastapi")
        },
    )


@router.get("/api/health", response_model=HealthResponse, operation_id="health_api_health_get")
def service_health(jobs: Jobs) -> HealthResponse:
    return health()


@router.post("/api/simulations/quick", response_model=SimulationResult)
def run_quick_simulation(request: SimulationRequest) -> SimulationResult:
    """同步运行一个快速单微网算例，用于首个真实端到端闭环。"""
    if request.scenario_type is not ScenarioType.SINGLE_MICROGRID or request.steps > 24:
        raise HTTPException(
            status_code=422,
            detail="quick simulation accepts single_microgrid with at most 24 time steps",
        )
    try:
        return run_simulation(request)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/api/simulations",
    response_model=JobStatus,
    status_code=202,
    responses={507: {"description": "任务存储已达容量上限或无法完整统计，暂不受理新任务"}},
)
def create_simulation(request: SimulationRequest, job_manager: Jobs) -> JobStatus:
    """创建独立进程仿真会话；同一时刻最多运行一个重型任务。"""
    try:
        return job_manager.create(request)
    except RetentionCapacityError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@router.get("/api/simulations/recent-single", response_model=list[JobStatus])
def recent_single_results(job_manager: Jobs) -> list[JobStatus]:
    return job_manager.recent_single_results()


@router.get("/api/patent2/inputs", response_model=Patent2Inputs)
def patent2_inputs(scenario: Patent2Scenario = "normal") -> Patent2Inputs:
    return describe_example(scenario)


@router.get("/api/single-example/inputs", response_model=SingleExampleInputs)
def single_example_inputs() -> SingleExampleInputs:
    return describe_single_example()


@router.get("/api/single-example/library", response_model=list[Literal["core"]])
def single_example_library() -> list[Literal["core"]]:
    return ["core"] if single_library.library_path().is_file() else []


@router.get("/api/single-example/library/{example}", response_model=SimulationResult)
def single_example_record(example: Literal["core"]) -> SimulationResult:
    try:
        return single_library.read_library()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="尚未保存单微网示范记录，请现场重新计算") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="保存记录不符合当前固定实施例或执行记录不完整，不能回放") from exc


@router.get("/api/patent2/runs", response_model=list[JobStatus])
def patent2_runs(job_manager: Jobs) -> list[JobStatus]:
    return job_manager.recent_patent2_results()


@router.get("/api/patent2/library", response_model=list[Patent2Scenario])
def patent2_library() -> list[Patent2Scenario]:
    return [s for s in ("normal", "low_energy") if library_path(s).is_file()]


@router.get("/api/patent2/library/{scenario}", response_model=SimulationResult)
def patent2_library_result(scenario: Patent2Scenario) -> SimulationResult:
    try:
        return read_library(scenario)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="尚未保存该工况的示范记录，请现场计算或导入记录") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="保存记录的结构或输入摘要不一致，不能回放") from exc


@router.post("/api/patent2/runs", response_model=JobStatus, status_code=202)
def create_patent2_run(body: Patent2RunRequest, job_manager: Jobs) -> JobStatus:
    request = SimulationRequest(
        name="专利二 · " + ("正常初始电量" if body.scenario == "normal" else "低电量恢复"),
        scenario_type="cluster_coordination", steps=4, patent2_case=body.scenario,
        storage_enabled=True, admm_max_iterations=1000,
        solver={"time_limit_seconds": 60, "formulation": "misocp"},
    )
    try:
        return job_manager.create(request)
    except RetentionCapacityError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@router.get("/api/simulations/{simulation_id}", response_model=JobStatus)
def get_simulation(simulation_id: str, job_manager: Jobs) -> JobStatus:
    try:
        return job_manager.get(simulation_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="仿真任务不存在或已按保留策略自动清理，请重新运行仿真"
        ) from exc


@router.post("/api/simulations/{simulation_id}/cancel", response_model=JobStatus)
def cancel_simulation(simulation_id: str, job_manager: Jobs) -> JobStatus:
    try:
        return job_manager.cancel(simulation_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="仿真任务不存在或已按保留策略自动清理，请重新运行仿真"
        ) from exc


@router.get("/api/simulations/{simulation_id}/result", response_model=SimulationResult)
def get_simulation_result(simulation_id: str, job_manager: Jobs) -> SimulationResult:
    try:
        return SimulationResult.model_validate_json(job_manager.read_result(simulation_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="仿真任务不存在或已按保留策略自动清理，请重新运行仿真"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="结果不符合当前契约；历史单微电网结果请通过归档环境读取",
        ) from exc


@router.get("/api/simulations/{simulation_id}/live")
def simulation_live(simulation_id: str, job_manager: Jobs, after: int = Query(default=0, ge=0), stream: str | None = None) -> dict:
    """Read committed display events; this never advances the simulation."""
    try:
        return job_manager.live(simulation_id, after, stream)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="仿真任务不存在") from exc


@router.websocket("/api/simulations/{simulation_id}/stream")
async def stream_simulation_status(
    websocket: WebSocket, simulation_id: str, job_manager: Jobs
) -> None:
    """推送真实任务阶段；断线只结束订阅，不影响独立求解进程。"""
    await websocket.accept()
    try:
        while True:
            try:
                status = job_manager.get(simulation_id)
            except KeyError:
                await websocket.send_json(
                    {
                        "simulation_id": simulation_id,
                        "state": "failed",
                        "error_code": "NOT_FOUND",
                        "error_message": "仿真任务不存在或已按保留策略自动清理，请重新运行仿真",
                    }
                )
                await websocket.close(code=4404)
                return
            await websocket.send_json(status.model_dump(mode="json"))
            if status.state.value not in {"queued", "running"}:
                await websocket.close(code=1000)
                return
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return


def create_app(
    root: Path | None = None,
    demo: DemoConfig | None = None,
    *,
    retention: RetentionPolicy = RetentionPolicy(),
    field_transport: ExecutionTransport | None = None,
    field_clock: Callable[[], datetime] | None = None,
    field_recovery_verifier: RecoveryVerifier | None = None,
) -> FastAPI:
    """Assemble the unified local API; all workers belong to its lifespan."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        demo_context = (
            live_demo(demo.bundle_path, demo.output, demo.interval)
            if demo is not None
            else nullcontext(None)
        )
        with demo_context as demo_runtime:
            manager = SimulationJobManager(root, retention=retention)
            pump = PeriodicWorker(manager.tick, 0.1)
            application.state.job_manager = manager
            application.state.job_pump = pump
            application.state.field_initializer = create_field_initializer(
                (root if root is not None else Path("artifacts")) / "field-initialization", transport=field_transport, clock=field_clock,
            )
            application.state.planning_updater = create_planning_updater(
                (root if root is not None else Path("artifacts")) / "field-initialization", clock=field_clock,
            )
            application.state.pcc_target_requester = create_pcc_target_requester(
                (root if root is not None else Path("artifacts")) / "field-initialization", clock=field_clock,
            )
            application.state.control_cycle_preparer = create_control_cycle_preparer(
                (root if root is not None else Path("artifacts")) / "field-initialization", transport=field_transport, clock=field_clock,
            )
            application.state.snapshot_submitter = create_snapshot_submitter(
                (root if root is not None else Path("artifacts")) / "field-initialization", clock=field_clock,
            )
            (application.state.execution_reporter, application.state.field_status_query,
             application.state.field_stopper, application.state.field_dispatcher) = create_execution_services(
                (root if root is not None else Path("artifacts")) / "field-initialization", transport=field_transport, clock=field_clock,
            )
            (application.state.recovery_preparer, application.state.run_resumer) = create_recovery_services(
                (root if root is not None else Path("artifacts")) / "field-initialization",
                clock=field_clock, transport=field_transport, verifier=field_recovery_verifier,
            )
            if demo_runtime is not None:
                application.state.demo_http_runtime = demo_runtime.http_runtime()
            try:
                pump.start()
                yield
            finally:
                try:
                    pump.close()
                finally:
                    manager.close()

    application = FastAPI(
        title="油田微电网新能源协同控制仿真服务",
        description="单机离线模拟数据服务；不连接真实 D5000、SCADA 或现场设备。",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    application.include_router(router)
    application.include_router(field_initialization_router)
    application.include_router(demo_device_router())
    return application


app = create_app()



def main() -> None:
    parser = argparse.ArgumentParser(description="Run the unified local simulation Web API")
    parser.add_argument(
        "--demo-bundle",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--demo-output",
        type=Path,
        default=Path("artifacts/system-demo-live"),
    )
    parser.add_argument("--demo-tick-seconds", type=float, default=1.0)
    args = parser.parse_args()
    bundle = args.demo_bundle if args.demo_bundle is not None else materialize_default_bundle(args.demo_output)
    demo = DemoConfig(bundle, args.demo_output, args.demo_tick_seconds)
    uvicorn.run(create_app(demo=demo), host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

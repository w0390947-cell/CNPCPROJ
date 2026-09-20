"""统一 FastAPI 网页入口：健康检查、仿真任务与持续模拟设备。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from importlib import metadata
from pathlib import Path
from typing import Annotated, cast

import cvxpy as cp
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from starlette.requests import HTTPConnection

from .bootstrap.demo import DemoConfig, demo_device_router, live_demo, materialize_default_bundle
from .job_manager import JobStatus, SimulationJobManager
from .runtime.periodic import PeriodicWorker
from .runtime.retention import RetentionCapacityError, RetentionPolicy
from .service import ScenarioType, SimulationRequest, SimulationResult, run_simulation


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

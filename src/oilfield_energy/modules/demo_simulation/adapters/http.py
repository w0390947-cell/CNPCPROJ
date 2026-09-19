"""Injected local HTTP transport. Never connects to a physical device."""
# pyright: reportUnusedFunction=false

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import JsonValue
from starlette.requests import HTTPConnection

from ..api import DemoService
from ..contracts import (
    Bundle,
    Catalog,
    Command,
    FaultRequest,
    Frame,
    JobsPort,
    Receipt,
    ResourceAvailability,
)


@dataclass(frozen=True)
class DemoHttpRuntime:
    """Started demo resources injected by a composition root."""

    service: DemoService
    bundle: Bundle
    check_running: Callable[[], None]


def _runtime(connection: HTTPConnection) -> DemoHttpRuntime:
    runtime = getattr(connection.app.state, "demo_http_runtime", None)
    if runtime is None:
        raise HTTPException(503, "模拟设备服务尚未启动")
    return cast(DemoHttpRuntime, runtime)


Runtime = Annotated[DemoHttpRuntime, Depends(_runtime)]


def _jobs(connection: HTTPConnection) -> JobsPort:
    jobs = getattr(connection.app.state, "demo_jobs", None)
    if jobs is None:
        raise HTTPException(503, "模拟仿真任务服务尚未启动")
    return cast(JobsPort, jobs)


Jobs = Annotated[JobsPort, Depends(_jobs)]


def device_routes() -> APIRouter:
    """Routes for the continuous synthetic devices, without job endpoints."""

    router = APIRouter()

    @router.get("/api/demo/catalog", response_model=Catalog)
    def catalog(runtime: Runtime) -> Catalog:
        return Catalog(
            dataset_id=runtime.bundle.dataset_id,
            source=runtime.bundle.source,
            presets=runtime.bundle.presets,
            devices=runtime.bundle.devices,
        )

    @router.get("/api/demo/telemetry", response_model=Frame)
    def telemetry(runtime: Runtime) -> Frame:
        runtime.check_running()
        return runtime.service.snapshot()

    @router.get("/api/demo/availability", response_model=ResourceAvailability)
    def availability(runtime: Runtime) -> ResourceAvailability:
        runtime.check_running()
        return runtime.service.availability()

    @router.post("/api/demo/commands", response_model=Receipt, status_code=202)
    def command(value: Command, runtime: Runtime) -> Receipt:
        runtime.check_running()
        try:
            return runtime.service.submit(value)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/demo/fault", response_model=Frame)
    def fault(value: FaultRequest, runtime: Runtime) -> Frame:
        runtime.check_running()
        runtime.service.fault(value.fault)
        return runtime.service.snapshot()

    @router.post("/api/demo/rearm")
    def rearm(runtime: Runtime) -> dict[str, bool]:
        runtime.check_running()
        return {"rearmed": runtime.service.rearm()}

    return router


def job_routes() -> APIRouter:
    """Standalone-demo job routes; the unified Web API owns its own job adapter."""

    router = APIRouter()

    @router.get("/api/health")
    def health(runtime: Runtime, jobs: Jobs) -> dict[str, JsonValue]:
        runtime.check_running()
        return jobs.health()

    @router.post("/api/simulations", status_code=202)
    def create(value: dict[str, JsonValue], jobs: Jobs) -> dict[str, JsonValue]:
        try:
            return jobs.create(value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/simulations/{job_id}")
    def status(job_id: str, jobs: Jobs) -> dict[str, JsonValue]:
        try:
            return jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(404, "simulation not found") from exc

    @router.post("/api/simulations/{job_id}/cancel")
    def cancel(job_id: str, jobs: Jobs) -> dict[str, JsonValue]:
        try:
            return jobs.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "simulation not found") from exc

    @router.get("/api/simulations/{job_id}/result")
    def result(job_id: str, jobs: Jobs) -> dict[str, JsonValue]:
        try:
            return jobs.result(job_id)
        except KeyError as exc:
            raise HTTPException(404, "simulation not found") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.websocket("/api/simulations/{job_id}/stream")
    async def job_stream(socket: WebSocket, job_id: str) -> None:
        await socket.accept()
        try:
            _runtime(socket).check_running()
            jobs = _jobs(socket)
            while True:
                try:
                    value = await asyncio.to_thread(jobs.get, job_id)
                except KeyError:
                    await socket.close(code=4404)
                    return
                await socket.send_json(value)
                if value["state"] not in ("running", "queued"):
                    await socket.close(code=1000)
                    return
                await asyncio.sleep(0.25)
        except WebSocketDisconnect:
            return

    return router


def routes() -> APIRouter:
    """Complete router retained for the standalone ``oilfield-demo`` service."""

    router = APIRouter()
    router.include_router(device_routes())
    router.include_router(job_routes())
    return router

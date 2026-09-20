"""Composition and lifetime of the local demonstration service."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import JsonValue

from oilfield_energy.modules.demo_simulation.adapters.http import (
    DemoHttpRuntime,
    device_routes,
    routes,
)
from oilfield_energy.modules.demo_simulation.api import DemoService, Fleet, verify_demo
from oilfield_energy.modules.demo_simulation.contracts import Bundle, JobsPort
from oilfield_energy.runtime.journal import RotatingJournal
from oilfield_energy.runtime.periodic import PeriodicWorker

from .adapters.demo_case import DemoNetwork, build_bundle, generate_bundle, load_bundle
from .adapters.demo_jobs import DemoJobs, run_demo_session


@dataclass(frozen=True)
class DemoConfig:
    """Validated infrastructure configuration for a synthetic-device service."""

    bundle_path: Path
    output: Path
    interval: float = 1.0

    def __post_init__(self) -> None:
        if not 0.05 <= self.interval <= 60:
            raise ValueError("wall interval must be in 0.05..60 seconds per simulated minute")


@dataclass(frozen=True)
class LiveDemo:
    """Resources owned by one running synthetic-device epoch."""

    bundle: Bundle
    raw: bytes
    run: Path
    service: DemoService
    jobs: JobsPort | None
    worker: PeriodicWorker

    def check_running(self) -> None:
        if self.worker.error is not None or monotonic() - self.worker.last_completed > max(
            10, self.worker.interval * 3
        ):
            raise HTTPException(503, "模拟设备服务停止，无法确认网络安全")

    def http_runtime(self) -> DemoHttpRuntime:
        return DemoHttpRuntime(self.service, self.bundle, self.check_running)


JobFactory = Callable[[Path, bytes], JobsPort]


def materialize_default_bundle(output: Path) -> Path:
    """Capture a content-addressed derivative of the packaged authority."""
    raw = build_bundle().model_dump_json(indent=2).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    folder = output / "inputs" / digest
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "bundle.json"
    if path.exists():
        _, captured = load_bundle(path)
        if captured != raw:
            raise ValueError("captured default input differs from its content address")
    else:
        path.write_bytes(raw)
        path.with_suffix(".sha256").write_text(digest + "\n", encoding="ascii")
    return path


def demo_device_router() -> APIRouter:
    """Construct the device HTTP adapter for a composed Web service."""

    return device_routes()


@contextmanager
def live_demo(
    bundle_path: Path,
    output: Path,
    interval: float = 1.0,
    job_factory: JobFactory | None = None,
) -> Iterator[LiveDemo]:
    """Start one demo epoch and close its worker, jobs and journal in order."""

    config = DemoConfig(bundle_path, output, interval)
    bundle, raw = load_bundle(config.bundle_path)
    epoch = uuid4().hex
    run = config.output / epoch
    run.mkdir(parents=True, exist_ok=False)
    (run / "bundle.json").write_bytes(raw)
    (run / "bundle.sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="ascii")
    log = RotatingJournal(run / "telemetry-and-receipts.jsonl")
    service = DemoService(Fleet(bundle, DemoNetwork(bundle), epoch), log.append)
    jobs: JobsPort | None = None
    worker: PeriodicWorker | None = None
    try:
        jobs = job_factory(run / "jobs", raw) if job_factory is not None else None

        def tick() -> None:
            service.tick()
            if jobs is not None:
                jobs.tick()

        worker = PeriodicWorker(tick, config.interval)
        runtime = LiveDemo(bundle, raw, run, service, jobs, worker)
        service.tick()
        worker.start()
        yield runtime
    finally:
        try:
            if worker is not None:
                worker.close()
        finally:
            try:
                if jobs is not None:
                    jobs.close()
            finally:
                log.close()


def create_demo_app(bundle_path: Path, output: Path, interval: float = 1.0) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with live_demo(bundle_path, output, interval, DemoJobs) as runtime:
            assert runtime.jobs is not None
            app.state.demo_http_runtime = runtime.http_runtime()
            app.state.demo_jobs = runtime.jobs
            yield

    app = FastAPI(title="Oilfield synthetic demo", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    # Routes are registered before startup so OpenAPI and clients see a stable contract.
    # Runtime resources are injected by the lifespan above.
    app.include_router(routes())
    return app


def verify_bundle(bundle_path: Path, output: Path) -> bool:
    bundle, raw = load_bundle(bundle_path)
    output.mkdir(parents=True, exist_ok=False)
    (output / "bundle.json").write_bytes(raw)
    (output / "bundle.sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="ascii")
    jobs = DemoJobs(output / "jobs", raw)

    def save(name: str, value: dict[str, JsonValue]) -> None:
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )

    try:
        report = verify_demo(
            bundle,
            jobs,
            DemoNetwork(bundle),
            save=save,
            clock=monotonic,
            wait=sleep,
            progress=lambda line: print(line, flush=True),
        )
        return report["passed"] is True
    finally:
        jobs.close()


__all__ = [
    "create_demo_app",
    "DemoConfig",
    "demo_device_router",
    "generate_bundle",
    "live_demo",
    "run_demo_session",
    "verify_bundle",
]

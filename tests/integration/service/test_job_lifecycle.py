"""Real queue/worker recovery, isolated roots; no browser polling dependency."""

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen

import pytest
import uvicorn
from websockets.sync.client import connect

from oilfield_energy.bootstrap.demo import DemoConfig, generate_bundle
from oilfield_energy.job_manager import SimulationJobManager
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation
from oilfield_energy.web_api import create_app


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def await_file(path: Path, expected: str = "succeeded", timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = read(path)
            if value["state"] == expected:
                return value
            assert value["state"] not in {"failed", "cancelled"}, value
        time.sleep(0.02)
    raise AssertionError(f"deadline reading {path}")


@contextmanager
def server(root: Path, demo: DemoConfig | None = None):
    app = create_app(root, demo)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        app.state.audit_port = sock.getsockname()[1]
        runner = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: runner.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not runner.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.02)

            def http(route: str, body: dict | None = None) -> dict:
                request = Request(
                    f"http://127.0.0.1:{sock.getsockname()[1]}" + route,
                    data=None if body is None else json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=10) as response:
                    return json.load(response)

            yield http, app
        finally:
            runner.should_exit = True
            thread.join(20)
            assert not thread.is_alive()


def test_unified_web_service_exposes_demo_devices_and_existing_api(tmp_path):
    generate_bundle(tmp_path / "bundle")
    demo = DemoConfig(
        tmp_path / "bundle/bundle.json",
        tmp_path / "demo-runs",
        interval=60,
    )
    with server(tmp_path / "jobs", demo) as (http, _app):
        assert http("/api/health")["service"] == "oilfield-energy-simulation"
        assert http("/api/demo/catalog")["dataset_id"] == "oilfield-unified-synthetic"
        frame = http("/api/demo/telemetry")
        assert frame["synthetic"] is True
        assert len(frame["devices"]) == 14
    assert list((tmp_path / "demo-runs").glob("*/telemetry-and-receipts.jsonl"))


def test_http_queue_completes_without_status_requests_or_subscribers(tmp_path):
    with server(tmp_path / "jobs") as (http, _app):
        a = http("/api/simulations", {"steps": 8})
        b = http("/api/simulations", {"steps": 8})
        assert b["state"] == "queued"
        # Disk-only observation: no HTTP manager call can advance this queue.
        await_file(tmp_path / "jobs" / b["simulation_id"] / "status.json")
        assert read(tmp_path / "jobs" / a["simulation_id"] / "status.json")["state"] == "succeeded"
        result = http(f"/api/simulations/{b['simulation_id']}/result")
        assert result["metadata"]["steps"] == 8


def test_service_shutdown_cancels_active_and_pending_work_and_releases_root(tmp_path):
    root = tmp_path / "jobs"
    with server(root) as (http, app):
        a = http("/api/simulations", {"steps": 96})
        process = app.state.job_manager._active_process
        b = http("/api/simulations", {"steps": 8})
        assert b["state"] == "queued"
    assert process.poll() is not None
    for item in (a, b):
        assert read(root / item["simulation_id"] / "status.json")["state"] == "cancelled"
    replacement = SimulationJobManager(root)
    replacement.close()


def test_websocket_disconnect_does_not_stop_the_queue(tmp_path):
    root = tmp_path / "jobs"
    with server(root) as (http, app):
        a = http("/api/simulations", {"steps": 8})
        b = http("/api/simulations", {"steps": 8})
        with connect(
            f"ws://127.0.0.1:{app.state.audit_port}/api/simulations/{a['simulation_id']}/stream"
        ) as ws:
            status = json.loads(ws.recv(timeout=5))
            assert status["simulation_id"] == a["simulation_id"]
            if status["state"] == "running":
                assert not status["result_available"]
        await_file(root / b["simulation_id"] / "status.json")


def test_second_coordinator_cannot_recover_a_live_owners_root(tmp_path):
    first = SimulationJobManager(tmp_path / "jobs")
    try:
        status = first.create(SimulationRequest(steps=96))
        with pytest.raises(RuntimeError, match="owned"):
            SimulationJobManager(tmp_path / "jobs")
        assert first.get(status.simulation_id).state.value == "running"
    finally:
        first.close()
    first.close()
    with pytest.raises(RuntimeError, match="closed"):
        first.create(SimulationRequest(steps=8))


def test_spawn_failure_does_not_poison_next_job(tmp_path):
    manager = SimulationJobManager(tmp_path / "jobs")
    try:
        with patch(
            "oilfield_energy.job_manager.subprocess.Popen",
            side_effect=OSError("injected"),
        ):
            failed = manager.create(SimulationRequest(steps=8))
        assert failed.state.value == "failed"
        assert failed.error_code == "WORKER_START_FAILED"
        successful = manager.create(SimulationRequest(steps=8))
        deadline = time.monotonic() + 60
        while successful.state.value in {"running", "queued"}:
            assert time.monotonic() < deadline
            time.sleep(0.05)
            successful = manager.get(successful.simulation_id)
        assert successful.state.value == "succeeded"
    finally:
        manager.close()


def test_worker_import_failure_is_actionable_and_releases_queue(tmp_path):
    manager = SimulationJobManager(tmp_path / "jobs", worker_module="nonexistent_audit_worker")
    try:
        failed = manager.create(SimulationRequest(steps=8))
        manager._worker_module = "oilfield_energy.job_worker"
        following = manager.create(SimulationRequest(steps=8))
        deadline = time.monotonic() + 60
        while following.state.value in {"queued", "running"}:
            assert time.monotonic() < deadline
            time.sleep(0.05)
            following = manager.get(following.simulation_id)
        assert following.state.value == "succeeded"
        failed_status = manager.get(failed.simulation_id)
        assert failed_status.error_code == "WORKER_IMPORT_FAILED"
        assert failed_status.error_message is not None
        assert "安装项目" in failed_status.error_message
    finally:
        manager.close()


def test_completed_legacy_directory_remains_readable_without_rewriting(tmp_path):
    root = tmp_path / "jobs"
    job_id = "c" * 32
    session = root / job_id
    session.mkdir(parents=True)
    # Existing directory contract: no execution manifest or attempts subdirectory.
    raw = run_simulation(SimulationRequest(steps=4)).model_dump_json().encode()
    (session / "result.json").write_bytes(raw)
    (session / "status.json").write_text(
        json.dumps(
            {
                "simulation_id": job_id,
                "name": "Synthetic historical layout",
                "state": "succeeded",
                "result_available": True,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:01:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    manager = SimulationJobManager(
        root, clock=lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    )
    try:
        restored = manager.read_result(job_id)
        assert restored == raw
        assert SimulationResult.model_validate_json(restored).metadata.steps == 4
        assert not (session / "attempts").exists()
    finally:
        manager.close()


def test_normal_cancellation_and_late_private_result_cannot_publish(tmp_path):
    manager = SimulationJobManager(tmp_path / "jobs")
    try:
        a = manager.create(SimulationRequest(steps=96))
        process, attempt = manager._active_process, manager._active_attempt
        b = manager.create(SimulationRequest(steps=8))
        assert manager.cancel(b.simulation_id).state.value == "cancelled"
        assert process.poll() is None
        assert manager.cancel(a.simulation_id).state.value == "cancelled"
        assert process.poll() is not None
        late = read(attempt / "status.json")
        late.update(state="succeeded", result_available=True)
        (attempt / "status.json").write_text(json.dumps(late), encoding="utf-8")
        (attempt / "result.json").write_text('{"late":true}', encoding="utf-8")
        manager.tick()
        assert manager.cancel(a.simulation_id).state.value == "cancelled"
        with pytest.raises(RuntimeError, match="not available"):
            manager.result_path(a.simulation_id)
        assert not (tmp_path / "jobs" / a.simulation_id / "result.json").exists()
    finally:
        manager.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle recovery experiment")
@pytest.mark.parametrize("solving", [False, True])
def test_owner_crash_stops_real_worker_and_fences_its_attempt(tmp_path, solving):
    helper = Path(__file__).resolve().parents[2] / "support/job_process_owner.py"
    command = [sys.executable, "-I", str(helper), str(tmp_path)]
    if solving:
        command.append("--solving")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = None
    with (tmp_path / "owner.log").open("w") as log:
        owner = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "ready.json").exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        ready = read(tmp_path / "ready.json")
        handle = kernel.OpenProcess(0x100001, False, ready["worker_pid"])
        assert handle
        (tmp_path / "crash").touch()
        owner.wait(timeout=10)
        manager = SimulationJobManager(tmp_path / "jobs")
        try:
            job_id = ready["simulation_id"]
            assert manager.get(job_id).state.value == "interrupted"
            assert kernel.WaitForSingleObject(handle, 10000) == 0, "orphan worker did not stop"
            attempt = Path(ready["attempt"])
            late = read(attempt / "status.json")
            late.update(state="succeeded", result_available=True)
            (attempt / "status.json").write_text(json.dumps(late), encoding="utf-8")
            (attempt / "result.json").write_text('{"late":true}', encoding="utf-8")
            assert manager.cancel(job_id).state.value == "interrupted"
            manager.tick()
            assert manager.get(job_id).state.value == "interrupted"
            with pytest.raises(RuntimeError, match="not available"):
                manager.result_path(job_id)
        finally:
            manager.close()
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=10)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 10000)
            kernel.CloseHandle(handle)

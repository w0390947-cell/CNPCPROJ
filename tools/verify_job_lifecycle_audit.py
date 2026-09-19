"""Item 14: real HTTP queue liveness and orphan-worker recovery on Windows.

Only output-root injection is used for the unmodified web API. No production
files or ordinary job directories are changed. All requests are synthetic.
"""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import oilfield_energy.job_manager as job_module
from oilfield_energy.job_manager import SimulationJobManager
from oilfield_energy.service import SimulationRequest


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def payload(steps: int = 8) -> dict:
    return {
        "name": "Synthetic lifecycle audit",
        "steps": steps,
        "scenario_type": "single_microgrid",
        "solver": {"time_limit_seconds": 60},
    }


def start(*args: str, log: Path) -> subprocess.Popen:
    with log.open("w", encoding="utf-8") as stream:
        return subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), *args],
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def wait_file(path: Path, predicate, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = read(path)
            if predicate(value):
                return value
        time.sleep(0.05)
    raise TimeoutError(str(path))


def queue_case(output: Path) -> dict:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = start(
        "--mode", "serve", "--output", str(output), "--port", str(port), log=output / "server.log"
    )
    base = f"http://127.0.0.1:{port}"

    def http(route: str, data: dict | None = None) -> dict:
        request = Request(
            base + route,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    ids = []
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                http("/api/health")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        first = http("/api/simulations", payload())
        ids.append(first["simulation_id"])
        second = http("/api/simulations", payload())
        ids.append(second["simulation_id"])
        assert second["state"] == "queued", second
        root = output / "jobs"
        first_done = wait_file(root / ids[0] / "status.json", lambda s: s["state"] == "succeeded")
        # Observe disk only: no status call, WebSocket or manager.tick during this interval.
        time.sleep(3)
        pending_without_subscribers = read(root / ids[1] / "status.json")
        polled = http("/api/simulations/" + ids[1])
        second_done = wait_file(root / ids[1] / "status.json", lambda s: s["state"] == "succeeded")
        result = http("/api/simulations/" + ids[1] + "/result")
        return {
            "first_terminal": first_done,
            "second_created": second,
            "second_after_first_completed_and_3s_idle": pending_without_subscribers,
            "second_after_one_status_request": polled,
            "second_terminal": second_done,
            "result_readable": result["metadata"]["steps"] == 8,
        }
    finally:
        # Cancel only this experiment's known IDs while its server still owns them.
        for job_id in ids:
            try:
                http("/api/simulations/" + job_id + "/cancel", {})
            except OSError:
                pass
        server.terminate()
        server.wait(timeout=10)


def orphan_case(output: Path) -> dict:
    parent = start("--mode", "orphan", "--output", str(output), log=output / "old_parent.log")
    manager = None
    handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        owner = wait_file(output / "orphan.json", lambda _s: True, 30)
        parent.wait(timeout=10)
        assert parent.returncode == 0
        # Hold an OS process handle: cleanup cannot accidentally target a reused PID.
        handle = kernel.OpenProcess(0x100001, False, owner["worker_pid"])
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        def alive() -> bool:
            return kernel.WaitForSingleObject(handle, 0) == 258

        assert alive(), "Worker completed before recovery experiment"
        manager = SimulationJobManager(output / "jobs")
        recovered = manager.get(owner["simulation_id"]).model_dump(mode="json")
        assert recovered["state"] == "interrupted", recovered
        path = output / "jobs" / owner["simulation_id"] / "status.json"
        resumed = wait_file(path, lambda s: s["state"] == "running", 30)
        cancelled = manager.cancel(owner["simulation_id"]).model_dump(mode="json")
        alive_after_cancel = alive()
        final = wait_file(path, lambda s: s["state"] in {"succeeded", "failed"})
        kernel.WaitForSingleObject(handle, 10000)
        return {
            "original_owner_exited": True,
            "worker_pid": owner["worker_pid"],
            "after_recovery": recovered,
            "old_worker_resumed": resumed,
            "cancel_response": cancelled,
            "worker_alive_after_cancel": alive_after_cancel,
            "later_status": final,
            "worker_exited": not alive(),
        }
    finally:
        if manager is not None:
            manager.close()
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 10000)
            kernel.CloseHandle(handle)
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=10)


def cancellation_control(output: Path) -> dict:
    manager = SimulationJobManager(output / "jobs")
    try:
        first = manager.create(SimulationRequest.model_validate(payload(96)))
        process = manager._active_process
        second = manager.create(SimulationRequest.model_validate(payload()))
        assert second.state.value == "queued"
        queued_cancel = manager.cancel(second.simulation_id)
        first_alive = process.poll() is None
        active_cancel = manager.cancel(first.simulation_id)
        time.sleep(1)
        final = manager.get(first.simulation_id)
        unavailable = False
        try:
            manager.result_path(first.simulation_id)
        except RuntimeError:
            unavailable = True
        assert queued_cancel.state.value == "cancelled" and first_alive
        assert active_cancel.state.value == final.state.value == "cancelled"
        assert process.poll() is not None and unavailable
        return {
            "queued_cancelled": True,
            "queued_cancel_preserves_active_worker": first_alive,
            "active_cancelled_and_process_exited": True,
            "cancelled_result_unavailable": unavailable,
            "repeated_cancel_stable": manager.cancel(first.simulation_id).state.value
            == "cancelled",
        }
    finally:
        manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["run", "serve", "orphan"], default="run")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.mode == "serve":
        import uvicorn

        # Inject an isolated persistence root without changing manager behavior.
        with patch(
            "oilfield_energy.job_manager.SimulationJobManager",
            lambda: SimulationJobManager(output / "jobs"),
        ):
            from oilfield_energy.web_api import app
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    elif args.mode == "orphan":
        manager = SimulationJobManager(output / "jobs")
        status = manager.create(SimulationRequest.model_validate(payload(96)))
        write(
            output / "orphan.json",
            {"simulation_id": status.simulation_id, "worker_pid": manager._active_process.pid},
        )
        # Deliberate process-owner crash; cleanup is handled by the outer audit.
        os._exit(0)
    else:
        output.mkdir(parents=True, exist_ok=False)
        summary = {"job_manager_import": job_module.__file__}
        for name, run in [
            ("http_queue", queue_case),
            ("orphan_recovery", orphan_case),
            ("normal_cancellation", cancellation_control),
        ]:
            child = output / name
            child.mkdir()
            summary[name] = run(child)
            write(output / "summary.json", summary)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

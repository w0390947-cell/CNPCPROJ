"""Managed stdin ownership must coexist with nested Windows spawn pools."""

import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import time

import pytest

from oilfield_energy.job_manager import SimulationJobManager
from oilfield_energy.service import SimulationRequest, SimulationResult


def test_real_managed_cluster_completes_windows_before_publishing(tmp_path):
    """The actual Web job-manager/worker boundary, not a direct solver call."""
    manager = SimulationJobManager(tmp_path / "jobs")
    completed = []
    try:
        status = manager.create(
            SimulationRequest(scenario_type="cluster_coordination", steps=24)
        )
        deadline = time.monotonic() + 600
        while status.state.value in {"queued", "running"}:
            assert time.monotonic() < deadline, status.model_dump_json()
            assert not status.result_available
            if status.rolling is not None:
                completed.append(status.rolling.completed_windows)
            time.sleep(0.2)
            status = manager.get(status.simulation_id)
        assert status.state.value == "succeeded", status.model_dump_json()
        assert status.result_available and status.rolling is not None
        assert status.rolling.completed_windows == status.rolling.total_windows == 96
        assert completed == sorted(completed) and max(completed) > 0
        result = SimulationResult.model_validate_json(
            manager.read_result(status.simulation_id)
        )
        assert result.cluster_execution is not None
        assert len(result.cluster_execution.rolling_updates) == 96
        assert all(w.adopted for w in result.cluster_execution.rolling_updates)
        assert result.executive_summary.overall_passed == all(
            i.passed for i in result.validation_items
        )
    finally:
        manager.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows standard-handle inheritance")
@pytest.mark.parametrize("ending", ["normal", "owner_eof", "owner_data", "cancel"])
def test_managed_worker_can_spawn_and_still_revoke_descendants(tmp_path, ending):
    script = tmp_path / "managed_pool.py"
    marker = tmp_path / "ready.json"
    script.write_text(
        """
import json, os, sys, time
from pathlib import Path
from oilfield_energy.runtime.parallel import OwnedProcessPool
from oilfield_energy.runtime.worker_lifetime import watch_owner_pipe
if __name__ == "__main__":
    watch_owner_pipe()
    assert sys.stdin.read() == ""
    with OwnedProcessPool(3) as pool:
        values = pool.map(abs, [-1, -2, -3])
        pids = pool.map(eval, ["(__import__('time').sleep(.1), __import__('os').getpid())[1]"] * 9)
        Path(sys.argv[1]).write_text(json.dumps({"owner": os.getpid(), "workers": sorted(set(pids)), "values": values}))
        if sys.argv[2] != "normal":
            pool.map(time.sleep, [60, 60, 60])
""",
        encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    with (tmp_path / "worker.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-I", str(script), str(marker), ending],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert process.poll() is None, (tmp_path / "worker.log").read_text()
            assert time.monotonic() < deadline, "managed nested pool did not start"
            time.sleep(0.05)
        ready = json.loads(marker.read_text())
        assert ready["values"] == [1, 2, 3]
        assert len(ready["workers"]) == 3
        if ending == "normal":
            assert process.wait(timeout=10) == 0
            return
        for pid in [ready["owner"], *ready["workers"]]:
            handle = kernel.OpenProcess(0x100000, False, pid)
            assert handle
            handles.append(handle)
        if ending == "owner_eof":
            process.stdin.close()
        elif ending == "owner_data":
            process.stdin.write(b"unexpected")
            process.stdin.flush()
        else:
            process.terminate()
            process.wait(timeout=5)
            process.stdin.close()
        process.wait(timeout=10)
        assert all(kernel.WaitForSingleObject(h, 10000) == 0 for h in handles)
    finally:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True
            )
            process.wait(timeout=10)
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        for handle in handles:
            kernel.CloseHandle(handle)

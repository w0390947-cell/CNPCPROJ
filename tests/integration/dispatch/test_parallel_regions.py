"""Independent regional processes preserve plans, failure and lifetime ownership."""

from dataclasses import replace
import json
import os
from pickle import dumps
import subprocess
import sys
import time

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.admm import run_admm_coordination
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    RegionalPlanRequest,
    realize_regional_plan,
)
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.runtime.parallel import OwnedProcessPool
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


def test_processes_are_isolated_ordered_and_propagate_failure():
    with OwnedProcessPool(3) as pool:
        # Built-in callable keeps this spawn test independent of pytest imports.
        codes = [
            f"(__import__('time').sleep(.2), __import__('os').getpid(), {i})[1:]"
            for i in range(6)
        ]
        results = pool.map(eval, codes)
        assert [r[1] for r in results] == list(range(6))
        assert all(r[0] != os.getpid() for r in results)
        assert len({r[0] for r in results}) >= 2
        with pytest.raises(ZeroDivisionError):
            pool.map(eval, ["1", "1/0", "3"])
        assert pool.map(abs, [-3, -2, -1]) == [3, 2, 1]
    with pytest.raises(RuntimeError, match="not open"):
        pool.map(abs, [-1])


def test_real_parallel_plans_match_serial_and_keep_infeasibility():
    case = build_synthetic_case(steps=4)
    original_input = dumps(case)
    central = solve_case_ac_consistent(case, time_limit_seconds=30)
    losses = {
        name: {"p_loss_mw": data["loss_mw"], "q_loss_mvar": data["reactive_loss_mvar"]}
        for name, data in central.optimization.microgrids.items()
    }
    admm = run_admm_coordination(case, loss_calibration=losses)
    assert admm.converged
    requests = [
        RegionalPlanRequest(
            case,
            m.name,
            admm.p_references_mw[m.name].copy(),
            admm.q_references_mvar[m.name].copy(),
            True,
            30,
            ExecutionPolicy(),
        )
        for m in case.microgrids
    ]
    serial = [realize_regional_plan(request) for request in requests]
    with OwnedProcessPool(3) as pool:
        parallel = pool.map(realize_regional_plan, requests)
        bad = replace(requests[0], p_target_mw=np.full(4, -100.0))
        failure = pool.map(realize_regional_plan, [bad])[0]
    for (a, _), (b, _) in zip(serial, parallel):
        assert a.status == b.status == "passed"
        np.testing.assert_allclose(a.p_mw, b.p_mw, atol=1e-5)
        np.testing.assert_allclose(a.q_mvar, b.q_mvar, atol=1e-5)
        assert a.economic_cost_cny == pytest.approx(b.economic_cost_cny, abs=0.01)
    assert failure[0].status == "violated" and failure[1] is None
    assert dumps(case) == original_input


def test_worker_crash_is_reported_as_runtime_failure():
    with OwnedProcessPool(2) as pool:
        with pytest.raises(RuntimeError):
            pool.map(eval, ["__import__('os')._exit(12)"])


def test_workers_stop_when_owner_is_terminated(tmp_path):
    marker = tmp_path / "workers.json"
    script = tmp_path / "owner.py"
    script.write_text(
        """
import json, sys, time
from pathlib import Path
from oilfield_energy.runtime.parallel import OwnedProcessPool
if __name__ == '__main__':
    with OwnedProcessPool(2) as pool:
        rows = pool.map(eval, ["(__import__('time').sleep(.1), __import__('os').getpid())[1]"] * 4)
        Path(sys.argv[1]).write_text(json.dumps(rows))
        pool.map(time.sleep, [30, 30])
""",
        encoding="utf-8",
    )
    owner = subprocess.Popen([sys.executable, str(script), str(marker)])
    pids = []
    try:
        deadline = time.monotonic() + 30
        while (
            not marker.exists() and owner.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        assert marker.exists(), "worker startup did not complete"
        pids = list(set(json.loads(marker.read_text())))
        owner.terminate()
        owner.wait(timeout=5)
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            for pid in pids:
                handle = kernel.OpenProcess(0x100000, False, pid)
                if handle:
                    try:
                        assert kernel.WaitForSingleObject(handle, 5000) == 0
                    finally:
                        kernel.CloseHandle(handle)
        else:
            for pid in pids:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    pytest.fail(f"worker {pid} survived owner death")
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)

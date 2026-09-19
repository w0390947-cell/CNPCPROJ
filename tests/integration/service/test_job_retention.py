"""Reclamation uses isolated synthetic directories; never production task data."""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from oilfield_energy.job_manager import JobState, JobStatus, SimulationJobManager, write_job_status
from oilfield_energy.runtime.periodic import PeriodicWorker
from oilfield_energy.runtime.retention import RetentionCapacityError, RetentionPolicy
from oilfield_energy.runtime.retention_store import tree_bytes
from oilfield_energy.service import SimulationRequest
from oilfield_energy.web_api import create_simulation, get_simulation_result

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def job(root, number, *, age=0, state=JobState.SUCCEEDED, size=100):
    job_id = f"{number:032x}"
    session = root / job_id
    session.mkdir(parents=True)
    end = NOW - timedelta(seconds=age)
    write_job_status(
        session,
        JobStatus(
            simulation_id=job_id,
            name="Synthetic retention fixture",
            state=state,
            created_at=(NOW - timedelta(days=30)).isoformat(),
            updated_at=end.isoformat(),
            result_available=state is JobState.SUCCEEDED,
        ),
    )
    (session / "result.json").write_bytes(b"x" * size)
    (session / "logs.txt").write_bytes(b"log" * size)
    attempt = session / "attempts" / ("f" * 32)
    attempt.mkdir(parents=True)
    (attempt / "result.json").write_bytes(b"a" * size)
    return job_id, session


@pytest.mark.parametrize(
    "state", [JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED]
)
def test_startup_cleans_existing_terminal_jobs_and_preserves_recent_bytes(tmp_path, state):
    old, old_path = job(tmp_path, 1, age=86400, state=state)
    recent, recent_path = job(tmp_path, 2, age=86399)
    original = (recent_path / "result.json").read_bytes()
    manager = SimulationJobManager(tmp_path, clock=lambda: NOW)
    try:
        assert not old_path.exists()
        with pytest.raises(KeyError):
            manager.get(old)
        assert manager.read_result(recent) == original
        assert (recent_path / "attempts").exists()
        assert (tmp_path / ".coordinator.lock").is_file()
        assert not list((tmp_path / ".retired").iterdir())
        assert '"reason": "expired"' in (tmp_path / "retention.jsonl").read_text()
    finally:
        manager.close()


def test_capacity_counts_logs_and_attempts_and_evicts_oldest_end_first(tmp_path):
    _, newer = job(tmp_path, 1, age=100)
    _, older = job(tmp_path, 2, age=200)
    limit = tree_bytes(newer)
    assert limit > (newer / "result.json").stat().st_size
    manager = SimulationJobManager(
        tmp_path, clock=lambda: NOW, retention=RetentionPolicy(max_bytes=limit)
    )
    try:
        assert newer.exists() and not older.exists()
        assert manager._storage_usage.bytes == limit
        assert '"reason": "capacity"' in (tmp_path / "retention.jsonl").read_text()
    finally:
        manager.close()


def test_recovery_starts_retention_at_interruption_and_preserves_live_jobs(tmp_path):
    interrupted, _ = job(tmp_path, 1, age=864000, state=JobState.RUNNING)
    manager = SimulationJobManager(tmp_path, clock=lambda: NOW)
    try:
        assert manager.get(interrupted).updated_at == NOW.isoformat()
        for number, state in [(2, JobState.QUEUED), (3, JobState.RUNNING)]:
            _, session = job(tmp_path, number, age=864000, state=state)
            manager.cleanup(force=True)
            assert session.exists()
        # An owned process is protected even if its private result is already complete.
        owned, session = job(tmp_path, 4, age=864000)
        manager._active_id = owned
        manager.cleanup(force=True)
        assert session.exists()
        manager._active_id = None
    finally:
        manager.close()


def test_periodic_cleanup_needs_no_browser_requests_and_is_throttled(tmp_path):
    now, monotonic = [NOW], [0.0]
    _, session = job(tmp_path, 1)
    manager = SimulationJobManager(
        tmp_path, clock=lambda: now[0], monotonic_clock=lambda: monotonic[0]
    )
    pump = PeriodicWorker(manager.tick, 0.01)
    try:
        now[0] += timedelta(days=1)
        with patch.object(manager._retention, "sweep", wraps=manager._retention.sweep) as sweep:
            manager.tick()
            assert sweep.call_count == 0
        monotonic[0] = 60
        pump.start()
        deadline = time.monotonic() + 5
        while session.exists():
            assert time.monotonic() < deadline and pump.error is None
            time.sleep(0.01)
    finally:
        pump.close()
        manager.close()


def test_partial_delete_is_charged_retried_after_restart_and_does_not_stop_queue(tmp_path):
    job_id, session = job(tmp_path, 1, age=86400)

    def partial(path):
        (path / "status.json").unlink()
        raise PermissionError("synthetic Windows sharing violation")

    with patch("oilfield_energy.runtime.retention_store.shutil.rmtree", side_effect=partial):
        manager = SimulationJobManager(tmp_path, clock=lambda: NOW)
    try:
        assert not session.exists()
        retired = tmp_path / ".retired" / job_id
        assert manager._storage_usage.bytes == tree_bytes(retired) > 0
        manager.tick()
        manager.close()
        replacement = SimulationJobManager(tmp_path, clock=lambda: NOW)
        try:
            assert not retired.exists()
            assert replacement._storage_usage.bytes == 0
        finally:
            replacement.close()
    finally:
        manager.close()


def test_failed_rename_keeps_complete_result_then_retries(tmp_path):
    job_id, session = job(tmp_path, 1, age=86400)
    raw = (session / "result.json").read_bytes()
    with patch("oilfield_energy.runtime.retention_store.Path.rename", side_effect=PermissionError):
        manager = SimulationJobManager(tmp_path, clock=lambda: NOW)
    try:
        assert manager.read_result(job_id) == raw
        manager.cleanup(force=True)
        assert not session.exists()
    finally:
        manager.close()


def test_result_reader_finishes_before_directory_is_retired(tmp_path):
    now = [NOW]
    job_id, session = job(tmp_path, 1)
    manager = SimulationJobManager(tmp_path, clock=lambda: now[0])
    entered, release = Event(), Event()
    original = Path.read_bytes

    def slow_read(path):
        if path == session / "result.json":
            entered.set()
            assert release.wait(5)
        return original(path)

    try:
        with ThreadPoolExecutor(2) as pool, patch.object(Path, "read_bytes", slow_read):
            reader = pool.submit(manager.read_result, job_id)
            assert entered.wait(5)
            now[0] += timedelta(days=1)
            cleanup = pool.submit(manager.cleanup, force=True)
            assert session.exists()
            release.set()
            assert reader.result(timeout=5) == b"x" * 100
            cleanup.result(timeout=5)
            assert not session.exists()
        with pytest.raises(HTTPException) as response:
            get_simulation_result(job_id, manager)
        assert response.value.status_code == 404
        assert "自动清理" in response.value.detail
    finally:
        release.set()
        manager.close()


def test_over_capacity_protects_running_work_and_http_rejects_new_submission(tmp_path):
    manager = SimulationJobManager(
        tmp_path, clock=lambda: NOW, retention=RetentionPolicy(max_bytes=1000)
    )
    try:
        _, session = job(tmp_path, 1, state=JobState.RUNNING, size=1000)
        with pytest.raises(HTTPException) as response:
            create_simulation(SimulationRequest(steps=4), manager)
        assert response.value.status_code == 507
        assert session.exists()
        assert len(list(tmp_path.glob("*/status.json"))) == 1
        assert manager._queue == [] and manager._active_process is None
    finally:
        manager.close()


def test_unrelated_data_and_invalid_status_are_not_deleted(tmp_path):
    root = tmp_path / "jobs"
    job_id, invalid = job(root, 1, age=86400)
    (invalid / "status.json").write_text("{}")
    external = tmp_path / "export.json"
    external.write_bytes(b"keep")
    unrelated = root / "manual-export"
    unrelated.mkdir()
    (unrelated / "data").write_bytes(b"keep")
    _, expired = job(root, 2, age=86400)
    manager = SimulationJobManager(root, clock=lambda: NOW)
    try:
        assert not expired.exists()
        assert invalid.exists() and unrelated.exists() and external.read_bytes() == b"keep"
        assert manager._storage_usage.bytes == tree_bytes(invalid)
    finally:
        manager.close()


def test_linked_directory_cannot_escape_root_or_be_deleted(tmp_path):
    root, outside = tmp_path / "jobs", tmp_path / "outside"
    root.mkdir()
    _, external = job(outside, 1, age=864000, state=JobState.RUNNING)
    link = root / ("a" * 32)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)], check=True, capture_output=True
        )
    else:
        link.symlink_to(external, target_is_directory=True)
    original = (external / "status.json").read_bytes()
    manager = SimulationJobManager(root, clock=lambda: NOW)
    try:
        assert (external / "status.json").read_bytes() == original
        with pytest.raises(RetentionCapacityError, match="统计"):
            manager.create(SimulationRequest(steps=4))
        assert external.exists() and link.exists()
    finally:
        manager.close()
        # Remove the link itself, never recursively remove its target.
        link.rmdir() if os.name == "nt" else link.unlink()


def test_batch_limit_and_journal_rotation_bound_cleanup_work(tmp_path):
    for number in range(5):
        job(tmp_path, number, age=86400)
    manager = SimulationJobManager(
        tmp_path, clock=lambda: NOW, retention=RetentionPolicy(batch_size=2)
    )
    try:
        assert len(list(tmp_path.glob("*/status.json"))) == 3
        manager.cleanup(force=True)
        assert len(list(tmp_path.glob("*/status.json"))) == 1
        store = manager._retention
        store.journal.maximum_bytes = 100
        for _ in range(10):
            store.record(NOW, "test", bytes=10)
        assert len(list(tmp_path.glob("retention.jsonl*"))) == 3
        manager.cleanup(force=True)
        assert not list(tmp_path.glob("*/status.json"))
    finally:
        manager.close()


def test_locked_retired_directory_does_not_starve_other_expired_jobs(tmp_path):
    retired_root = tmp_path / ".retired"
    _, blocked = job(retired_root, 1, age=86400)
    _, expired = job(tmp_path, 2, age=86400)
    from shutil import rmtree

    def erase(path):
        if path == blocked:
            raise PermissionError("synthetic locked file")
        rmtree(path)

    with patch("oilfield_energy.runtime.retention_store.shutil.rmtree", side_effect=erase):
        manager = SimulationJobManager(
            tmp_path, clock=lambda: NOW, retention=RetentionPolicy(batch_size=1)
        )
    try:
        assert blocked.exists() and not expired.exists()
    finally:
        manager.close()


def test_full_disk_journal_failure_does_not_prevent_reclamation(tmp_path):
    _, expired = job(tmp_path, 1, age=86400)
    with patch(
        "oilfield_energy.runtime.retention_store.RotatingJournal",
        side_effect=OSError("synthetic disk full"),
    ):
        manager = SimulationJobManager(tmp_path, clock=lambda: NOW)
    try:
        assert not expired.exists()
        manager.cleanup(force=True)
        assert (tmp_path / "retention.jsonl").exists()
    finally:
        manager.close()


def test_nested_link_is_not_followed_during_capacity_scan_or_deletion(tmp_path):
    root = tmp_path / "jobs"
    _, expired = job(root, 1, age=86400)
    outside = tmp_path / "external-export"
    outside.mkdir()
    exported = outside / "result.json"
    exported.write_bytes(b"preserve exported result")
    link = expired / "linked-export"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    manager = SimulationJobManager(root, clock=lambda: NOW)
    try:
        assert expired.exists()
        assert not manager._storage_usage.complete
        assert exported.read_bytes() == b"preserve exported result"
    finally:
        manager.close()
        link.rmdir() if os.name == "nt" else link.unlink()


def test_storage_admission_recovers_when_running_task_becomes_terminal(tmp_path):
    manager = SimulationJobManager(
        tmp_path, clock=lambda: NOW, retention=RetentionPolicy(max_bytes=2000)
    )
    try:
        job_id, session = job(tmp_path, 1, state=JobState.RUNNING, size=1000)
        with pytest.raises(RetentionCapacityError):
            manager.create(SimulationRequest(steps=4))
        status = manager.get(job_id)
        write_job_status(session, status.model_copy(update={"state": JobState.FAILED}))
        with patch("oilfield_energy.job_manager.subprocess.Popen", side_effect=OSError("test")):
            admitted = manager.create(SimulationRequest(steps=4))
        assert admitted.simulation_id != job_id
        assert admitted.error_code == "WORKER_START_FAILED"
        assert not session.exists()
    finally:
        manager.close()

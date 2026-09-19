"""单机网页仿真任务队列与独立工作进程管理。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .runtime.job_lease import JobRootLease
from .runtime.retention import RetentionCapacityError, RetentionPolicy
from .runtime.retention_store import RetentionStore, StorageUsage, is_job_id, is_plain_directory
from .service import ProgressUpdate, SimulationRequest, SimulationResult


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    simulation_id: str
    name: str
    state: JobState
    created_at: str
    updated_at: str
    stage: str | None = None
    stage_label: str | None = None
    stage_sequence: int | None = None
    total_stages: int = Field(default=6, ge=1)
    result_available: bool = False
    error_code: str | None = None
    error_message: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    # Windows readers/virus scanners may briefly hold a handle without delete sharing.
    # Retry only atomic replacement; never expose a truncated status document.
    for attempt in range(8):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if os.name != "nt" or attempt == 7:
                raise
            time.sleep(0.01 * (attempt + 1))


def write_job_status(session_dir: Path, status: JobStatus) -> None:
    atomic_write_json(session_dir / "status.json", status.model_dump(mode="json"))


def progress_status(base: JobStatus, update: ProgressUpdate) -> JobStatus:
    return base.model_copy(
        update={
            "state": JobState.RUNNING,
            "updated_at": utc_now(),
            "stage": update.stage.value,
            "stage_label": update.label,
            "stage_sequence": update.sequence,
            "total_stages": update.total_stages,
        }
    )


def worker_exit_failure(log_path: Path, exit_code: int) -> tuple[str, str]:
    """Translate a process-level failure without exposing raw worker output."""
    try:
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-16_384:]
    except OSError:
        log_tail = ""
    if "Error while finding module specification for" in log_tail or "No module named " in log_tail:
        return (
            "WORKER_IMPORT_FAILED",
            "仿真工作进程无法导入项目包；请在当前 Python 环境安装项目后重启仿真服务",
        )
    return "WORKER_EXITED", f"仿真工作进程异常退出，代码 {exit_code}"


class SimulationJobManager:
    """最多运行一个重型子进程，其余会话按创建顺序排队。"""

    def __init__(
        self,
        root: Path | None = None,
        *,
        input_bundle: bytes | None = None,
        worker_module: str = "oilfield_energy.job_worker",
        retention: RetentionPolicy = RetentionPolicy(),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.root = (root or project_root / "results" / "web_sessions").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._maintenance_lock = RLock()
        self._clock, self._monotonic = clock, monotonic_clock
        self._next_cleanup = 0.0
        self._retention_policy = retention
        self._storage_usage = StorageUsage()
        self._input_bundle = input_bundle
        self._worker_module = worker_module
        self._queue: list[str] = []
        self._active_id: str | None = None
        self._active_process: subprocess.Popen[str] | None = None
        self._active_attempt: Path | None = None
        self._closed = False
        self._lease = JobRootLease(self.root)
        self._retention: RetentionStore | None = None
        try:
            self._recover_existing_sessions()
            self._retention = RetentionStore(self.root, retention)
            self._retention.record(self._clock(), "configured", policy=retention.model_dump())
            self.cleanup(force=True)
        except Exception:
            if self._retention is not None:
                self._retention.close()
            self._lease.close()
            raise

    def _session_dir(self, simulation_id: str) -> Path:
        if not simulation_id or any(
            character not in "0123456789abcdef" for character in simulation_id
        ):
            raise KeyError("unknown simulation")
        lexical = self.root / simulation_id
        candidate = lexical.resolve()
        if candidate.parent != self.root or candidate != lexical or lexical.is_symlink():
            raise KeyError("unknown simulation")
        return candidate

    def _read_status(self, simulation_id: str) -> JobStatus:
        path = self._session_dir(simulation_id) / "status.json"
        if not path.is_file():
            raise KeyError("unknown simulation")
        return JobStatus.model_validate_json(path.read_text(encoding="utf-8"))

    def _recover_existing_sessions(self) -> None:
        for status_path in self.root.glob("*/status.json"):
            try:
                if not is_job_id(status_path.parent.name) or not is_plain_directory(
                    status_path.parent
                ):
                    continue
                status = JobStatus.model_validate_json(status_path.read_text(encoding="utf-8"))
                if status.simulation_id != status_path.parent.name:
                    continue
            except (OSError, ValueError):
                continue
            if status.state in {JobState.RUNNING, JobState.QUEUED}:
                write_job_status(
                    status_path.parent,
                    status.model_copy(
                        update={
                            "state": JobState.INTERRUPTED,
                            "stage": "interrupted",
                            "stage_label": "服务重启，任务已中断",
                            "result_available": False,
                            "updated_at": self._clock().isoformat(),
                            "error_code": "SERVICE_RESTARTED",
                            "error_message": "网页服务重启，原工作进程状态已失效",
                        }
                    ),
                )

    def _refresh(self) -> None:
        if self._closed:
            return
        if self._active_process is not None:
            self._collect_active()
        if self._active_process is None and self._queue:
            self._start_next()

    def _collect_active(self) -> None:
        """Only the live coordinator publishes canonical status and results."""
        process, simulation_id, attempt = (
            self._active_process,
            self._active_id,
            self._active_attempt,
        )
        assert process is not None and simulation_id is not None and attempt is not None
        exit_code = process.poll()
        current = self._read_status(simulation_id)
        try:
            reported = JobStatus.model_validate_json((attempt / "status.json").read_bytes())
            if reported.simulation_id != simulation_id:
                raise ValueError("worker status identity mismatch")
        except (OSError, ValueError):
            reported = None
        session = self._session_dir(simulation_id)
        if current.state is JobState.RUNNING:
            if exit_code is None:
                if (
                    reported is not None
                    and reported.state is JobState.RUNNING
                    and reported != current
                ):
                    write_job_status(session, reported)
            elif exit_code == 0 and reported is not None and reported.state is JobState.SUCCEEDED:
                try:
                    result = SimulationResult.model_validate_json(
                        (attempt / "result.json").read_bytes()
                    )
                    atomic_write_json(session / "result.json", result.model_dump(mode="json"))
                    provenance = attempt / "input_provenance.json"
                    if provenance.is_file():
                        atomic_write_json(
                            session / provenance.name,
                            json.loads(provenance.read_bytes()),
                        )
                    write_job_status(
                        session,
                        reported.model_copy(
                            update={
                                "result_available": True,
                                "updated_at": self._clock().isoformat(),
                            }
                        ),
                    )
                except (OSError, ValueError):
                    self._fail_job(current, "INVALID_WORKER_RESULT", "工作进程未提供完整有效结果")
            elif reported is not None and reported.state is JobState.FAILED:
                write_job_status(
                    session, reported.model_copy(update={"updated_at": self._clock().isoformat()})
                )
            else:
                code, message = worker_exit_failure(session / "logs.txt", exit_code)
                self._fail_job(current, code, message)
        if exit_code is not None:
            if process.stdin is not None:
                process.stdin.close()
            self._active_id = None
            self._active_process = None
            self._active_attempt = None

    def _fail_job(self, status: JobStatus, code: str, message: str) -> None:
        write_job_status(
            self._session_dir(status.simulation_id),
            status.model_copy(
                update={
                    "state": JobState.FAILED,
                    "stage": "failed",
                    "stage_label": "仿真失败",
                    "updated_at": self._clock().isoformat(),
                    "result_available": False,
                    "error_code": code,
                    "error_message": message,
                }
            ),
        )

    def _start_next(self) -> None:
        simulation_id = self._queue.pop(0)
        session_dir = self._session_dir(simulation_id)
        status = self._read_status(simulation_id)
        running = status.model_copy(
            update={
                "state": JobState.RUNNING,
                "updated_at": self._clock().isoformat(),
                "stage": "worker_starting",
                "stage_label": "正在启动独立仿真进程",
            }
        )
        write_job_status(session_dir, running)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        try:
            # Old workers never receive the canonical directory as their output.
            attempt = session_dir / "attempts" / uuid4().hex
            attempt.mkdir(parents=True, exist_ok=False)
            atomic_write_json(
                session_dir / "execution.json",
                {
                    "schema_version": "job-execution-v1",
                    "attempt_id": attempt.name,
                    "publication": "coordinator_only",
                },
            )
            for filename in ("request.json", "bundle.json", "bundle.sha256"):
                source = session_dir / filename
                if source.is_file():
                    shutil.copyfile(source, attempt / filename)
            write_job_status(attempt, running)
            with (session_dir / "logs.txt").open("a", encoding="utf-8") as log_handle:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        self._worker_module,
                        "--session",
                        str(attempt),
                        "--managed",
                    ],
                    cwd=attempt,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
        except OSError:
            self._fail_job(running, "WORKER_START_FAILED", "无法启动仿真工作进程")
            return
        self._active_id = simulation_id
        self._active_process = process
        self._active_attempt = attempt

    def create(self, request: SimulationRequest) -> JobStatus:
        # Keep admissions serialized with maintenance, but not with result reads
        # during recursive erasure. Lock order is always maintenance -> state.
        with self._maintenance_lock:
            self.cleanup(force=True)
            with self._lock:
                if self._closed:
                    raise RuntimeError("simulation job manager is closed")
                if not self._storage_usage.complete:
                    raise RetentionCapacityError(
                        "无法完整统计仿真任务占用，请检查任务目录权限及清理日志后重试"
                    )
                if self._storage_usage.bytes >= self._retention_policy.max_bytes:
                    raise RetentionCapacityError(
                        "仿真任务存储已达容量上限，自动清理后仍无足够空间；请等待运行任务结束或检查清理日志后重试"
                    )
                return self._create(request)

    def _create(self, request: SimulationRequest) -> JobStatus:
        with self._lock:
            if self._closed:
                raise RuntimeError("simulation job manager is closed")
            self._refresh()
            simulation_id = uuid4().hex
            session_dir = self._session_dir(simulation_id)
            session_dir.mkdir(parents=True, exist_ok=False)
            atomic_write_json(session_dir / "request.json", request.model_dump(mode="json"))
            if self._input_bundle is not None:
                (session_dir / "bundle.json").write_bytes(self._input_bundle)
                (session_dir / "bundle.sha256").write_text(
                    hashlib.sha256(self._input_bundle).hexdigest(), encoding="ascii"
                )
            now = self._clock().isoformat()
            status = JobStatus(
                simulation_id=simulation_id,
                name=request.name,
                state=JobState.QUEUED,
                created_at=now,
                updated_at=now,
                stage="queued",
                stage_label="等待仿真进程",
            )
            write_job_status(session_dir, status)
            self._queue.append(simulation_id)
            self._refresh()
            return self._read_status(simulation_id)

    def get(self, simulation_id: str) -> JobStatus:
        with self._lock:
            self._refresh()
            return self._read_status(simulation_id)

    def tick(self) -> None:
        with self._lock:
            self._refresh()
        self.cleanup()

    def _finished_at(self, simulation_id: str) -> datetime | None:
        with self._lock:
            if simulation_id == self._active_id or simulation_id in self._queue:
                return None
            status = self._read_status(simulation_id)
            if status.simulation_id != simulation_id:
                raise ValueError("job status identity mismatch")
            if status.state in {JobState.QUEUED, JobState.RUNNING}:
                return None
            # Terminal updated_at is immutable; old results need no schema migration.
            end = datetime.fromisoformat(status.updated_at.replace("Z", "+00:00"))
            if end.utcoffset() is None:
                raise ValueError("terminal timestamp has no timezone")
            return end

    def _retire(self, simulation_id: str, expected_end: datetime) -> bool:
        with self._lock:
            if self._closed or self._finished_at(simulation_id) != expected_end:
                return False
            assert self._retention is not None
            self._retention.retire(simulation_id)
            return True

    def cleanup(self, *, force: bool = False) -> None:
        """Startup/submission sweep, otherwise at most once per configured interval."""
        with self._maintenance_lock:
            if self._closed or (not force and self._monotonic() < self._next_cleanup):
                return
            self._next_cleanup = self._monotonic() + self._retention_policy.interval_seconds
            assert self._retention is not None
            self._storage_usage = self._retention.sweep(
                self._clock(), self._finished_at, self._retire
            )

    def close(self) -> None:
        with self._maintenance_lock:
            self._close()

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            pending = list(self._queue)
            self._queue.clear()
            try:
                if self._active_id is not None:
                    self.cancel(self._active_id)
                for job_id in pending:
                    self.cancel(job_id)
            finally:
                self._closed = True
                try:
                    if self._retention is not None:
                        self._retention.close()
                finally:
                    self._lease.close()

    def read_result(self, simulation_id: str) -> bytes:
        """Read a complete immutable snapshot before retention can retire its job."""
        with self._lock:
            return self.result_path(simulation_id).read_bytes()

    def result_path(self, simulation_id: str) -> Path:
        """Legacy lookup only; use read_result for a concurrency-safe snapshot."""
        with self._lock:
            self._refresh()
            status = self._read_status(simulation_id)
            path = self._session_dir(simulation_id) / "result.json"
            if status.state is not JobState.SUCCEEDED or not path.is_file():
                raise RuntimeError("simulation result is not available")
            return path

    def cancel(self, simulation_id: str) -> JobStatus:
        with self._lock:
            if self._closed:
                raise RuntimeError("simulation job manager is closed")
            self._refresh()
            status = self._read_status(simulation_id)
            if status.state in {
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.INTERRUPTED,
            }:
                return status
            if simulation_id in self._queue:
                self._queue.remove(simulation_id)
            if simulation_id == self._active_id and self._active_process is not None:
                self._active_process.terminate()
                try:
                    self._active_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._active_process.kill()
                    self._active_process.wait(timeout=5)
                self._active_id = None
                if self._active_process.stdin is not None:
                    self._active_process.stdin.close()
                self._active_process = None
                self._active_attempt = None
            cancelled = status.model_copy(
                update={
                    "state": JobState.CANCELLED,
                    "updated_at": self._clock().isoformat(),
                    "stage": "cancelled",
                    "stage_label": "仿真已取消",
                    "error_code": None,
                    "error_message": None,
                }
            )
            write_job_status(self._session_dir(simulation_id), cancelled)
            self._refresh()
            return cancelled


__all__ = [
    "JobState",
    "JobStatus",
    "SimulationJobManager",
    "atomic_write_json",
    "progress_status",
    "utc_now",
    "worker_exit_failure",
    "write_job_status",
]

"""Local job-directory reclamation. Caller owns the root lease and serializes sweeps."""

import json
import logging
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .journal import RotatingJournal
from .retention import RetentionPolicy

LOGGER = logging.getLogger(__name__)


def is_job_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{32}", value) is not None


def is_plain_directory(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def tree_bytes(path: Path) -> int:
    """Never follow symlinks/junctions, including ones nested inside a job."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError("linked/reparse entries are not managed job storage")
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("special files are not managed job storage")
    total = 0
    for child in path.iterdir():
        try:
            total += tree_bytes(child)
        except FileNotFoundError:
            # A live worker may atomically replace its own temporary status file.
            continue
    return total


@dataclass(frozen=True)
class StorageUsage:
    bytes: int = 0
    complete: bool = True


class RetentionStore:
    """Quarantine before erasure; retry partial erasures after process restart.

    ``finished_at`` and ``retire`` are injected coordinator operations. The latter
    rechecks terminal ownership and renames under the same lock as result readers.
    Heavy recursive deletion takes place after that lock has been released.
    """

    def __init__(self, root: Path, policy: RetentionPolicy) -> None:
        self.root, self.policy = root.resolve(), policy
        self.retired = self.root / ".retired"
        if self.retired.exists() and not is_plain_directory(self.retired):
            raise ValueError("retention quarantine must be a plain directory")
        self.retired.mkdir(exist_ok=True)
        journal_path = self.root / "retention.jsonl"
        for path in (journal_path, *self.root.glob("retention.jsonl.*")):
            if path.is_symlink() or (
                path.exists()
                and getattr(path.lstat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("retention journal cannot be a link")
        self.journal: RotatingJournal | None = None

    def record(self, now: datetime, action: str, job_id: str = "", **details: object) -> None:
        try:
            if self.journal is None:
                self.journal = RotatingJournal(
                    self.root / "retention.jsonl", maximum_bytes=262_144, backups=2
                )
            self.journal.append(
                json.dumps(
                    dict(at=now.isoformat(), action=action, simulation_id=job_id, **details),
                    ensure_ascii=False,
                )
            )
        except OSError:
            LOGGER.warning("Could not write retention journal", exc_info=True)
            if self.journal is not None:
                try:
                    self.journal.close()
                except OSError:
                    pass  # The original write failure was reported above.
                self.journal = None

    def directories(self, parent: Path) -> list[Path]:
        if parent.resolve() not in (self.root, self.retired):
            raise ValueError("job storage escaped the owned root")
        if not is_plain_directory(parent):
            raise ValueError("job storage must be a plain directory")
        return sorted((p for p in parent.iterdir() if is_job_id(p.name)), key=lambda p: p.name)

    def retire(self, job_id: str) -> None:
        """Caller must hold its publication/read lock and confirm terminal state."""
        if not is_job_id(job_id):
            raise ValueError("invalid job directory identity")
        source, target = self.root / job_id, self.retired / job_id
        if source.resolve().parent != self.root or not is_plain_directory(source):
            raise ValueError("job directory escaped the owned root")
        if self.retired.resolve() != self.retired or not is_plain_directory(self.retired):
            raise ValueError("quarantine escaped the owned root")
        if target.exists():
            raise FileExistsError("job already has a quarantined directory")
        source.rename(target)

    def _erase(self, path: Path, now: datetime) -> bool:
        try:
            # Recheck the resolved absolute destination immediately before rmtree.
            if (
                path.parent != self.retired
                or path.resolve().parent != self.retired
                or not is_job_id(path.name)
                or not is_plain_directory(self.retired)
                or not is_plain_directory(path)
            ):
                raise ValueError("invalid quarantine deletion target")
            size = tree_bytes(path)
            shutil.rmtree(path)
            self.record(now, "deleted", path.name, bytes=size)
            return True
        except (OSError, ValueError) as exc:
            self.record(now, "delete_retry", path.name, error=str(exc))
            return False

    def measure(self, now: datetime) -> StorageUsage:
        total, complete = 0, True
        for parent in (self.root, self.retired):
            try:
                for path in self.directories(parent):
                    try:
                        total += tree_bytes(path)
                    except FileNotFoundError:
                        continue
                    except (OSError, ValueError) as exc:
                        complete = False
                        self.record(now, "scan_skipped", path.name, error=str(exc))
            except (OSError, ValueError) as exc:
                complete = False
                self.record(now, "scan_failed", error=str(exc))
        return StorageUsage(total, complete)

    def sweep(
        self,
        now: datetime,
        finished_at: Callable[[str], datetime | None],
        retire: Callable[[str, datetime], bool],
    ) -> StorageUsage:
        remaining = self.policy.batch_size
        try:
            for path in self.directories(self.retired):
                if remaining == 0:
                    break
                if self._erase(path, now):
                    remaining -= 1
            usage = self.measure(now)
            total = usage.bytes
            candidates: list[tuple[datetime, str, int]] = []
            for path in self.directories(self.root):
                try:
                    if not is_plain_directory(path):
                        continue
                    end = finished_at(path.name)
                    if end is not None:
                        candidates.append((end, path.name, tree_bytes(path)))
                except (KeyError, OSError, ValueError) as exc:
                    self.record(now, "candidate_skipped", path.name, error=str(exc))
            for end, job_id, size in sorted(candidates):
                reason = self.policy.reason(end, now, total)
                if remaining == 0:
                    break
                if reason is None:
                    continue
                try:
                    if retire(job_id, end):
                        remaining -= 1
                        self.record(now, "retired", job_id, reason=reason, bytes=size)
                        if self._erase(self.retired / job_id, now):
                            total -= size
                except (OSError, ValueError) as exc:
                    self.record(now, "retire_retry", job_id, error=str(exc))
        except (OSError, ValueError) as exc:
            self.record(now, "sweep_retry", error=str(exc))
        # Partial erasure is still charged. Never subtract bytes merely on rename.
        usage = self.measure(now)
        self.record(
            now,
            "sweep",
            bytes=usage.bytes,
            complete=usage.complete,
            over_capacity=usage.bytes > self.policy.max_bytes,
        )
        return usage

    def close(self) -> None:
        if self.journal is not None:
            self.journal.close()

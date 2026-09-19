"""Single coordinator lease, released by the OS even on process-owner death."""

import os
from pathlib import Path


class JobRootLease:
    """Non-blocking local-filesystem lease. The file must never be unlinked."""

    def __init__(self, root: Path) -> None:
        self._handle = (root / ".coordinator.lock").open("a+b")
        try:
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            raise RuntimeError("simulation job root already owned or cannot be locked") from exc

    def close(self) -> None:
        self._handle.close()

"""Managed worker entrypoint guard; stdin is an exclusive owner-lifetime pipe."""

import os
import sys
from threading import Thread


def _watch_owner(descriptor: int) -> None:
    try:
        # Read the raw descriptor: holding Python's buffered stdin lock in a
        # daemon thread prevents a normally completing interpreter from exiting.
        os.read(descriptor, 1)
    finally:
        # The coordinator sends no data. EOF, unexpected data or pipe failure
        # revokes this process, even while the solver occupies the main thread.
        # Workers write only attempt-local files, so abrupt exit cannot publish.
        os._exit(74)


def watch_owner_pipe() -> None:
    """Arm only for child processes explicitly launched with managed stdin."""
    Thread(
        target=_watch_owner, args=(sys.stdin.fileno(),), daemon=True, name="simulation-owner-pipe"
    ).start()

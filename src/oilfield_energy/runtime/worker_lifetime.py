"""Managed worker entrypoint guard; stdin is an exclusive owner-lifetime pipe."""

import os
import sys
from threading import Thread


def _detach_windows_stdin() -> int:
    """Keep the owner pipe private; descendants must inherit a quiet stdin.

    Windows can block interpreter startup while initializing a standard stream
    whose pipe already has a pending ReadFile in another process (CPython #34780).
    Detach both Python's stream and the OS standard handle BEFORE starting the
    blocking watcher. The duplicated, non-inheritable descriptor owns the pipe.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt

    original = sys.stdin
    if original is None:
        raise RuntimeError("managed worker requires an owner pipe")
    descriptor = os.dup(original.fileno())
    try:
        quiet_stdin = open(os.devnull, encoding="utf-8")
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
            kernel.SetStdHandle.restype = wintypes.BOOL
            if not kernel.SetStdHandle(
                -10 & 0xFFFFFFFF, msvcrt.get_osfhandle(quiet_stdin.fileno())
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            original.close()
            sys.stdin = quiet_stdin
        except BaseException:
            quiet_stdin.close()
            raise
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


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
    if sys.stdin is None:
        raise RuntimeError("managed worker requires an owner pipe")
    descriptor = _detach_windows_stdin() if os.name == "nt" else sys.stdin.fileno()
    Thread(
        target=_watch_owner,
        args=(descriptor,),
        daemon=True,
        name="simulation-owner-pipe",
    ).start()

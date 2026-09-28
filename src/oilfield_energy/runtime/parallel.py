"""Bounded spawned workers with explicit ownership and ordered results."""

from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context, parent_process
from multiprocessing.connection import wait
import os
from threading import Thread
from types import TracebackType
from typing import TypeVar

Input = TypeVar("Input")
Output = TypeVar("Output")


def _initialize_worker() -> None:
    # The owner can be forcibly cancelled while a native solver holds the main
    # thread. Its sentinel revokes every worker, including an in-flight solve.
    parent = parent_process()
    if parent is None:
        raise RuntimeError("parallel worker requires an owner process")
    sentinel = parent.sentinel

    def watch() -> None:
        wait([sentinel])
        os._exit(74)

    Thread(target=watch, daemon=True, name="numerical-owner-watch").start()


def _execute_isolated(function: Callable[[Input], Output], value: Input) -> Output:
    from threadpoolctl import threadpool_limits

    # Unpickling the callable has now imported its numerical backend. Limiting
    # pools in the initializer could run before NumPy/SCIP have been imported.
    with threadpool_limits(limits=1):
        return function(value)


class OwnedProcessPool:
    """One job's executor. Independent instances, bounded CPU, stable ordering.

    A single-worker configuration runs inline for reference/failure-path tests.
    Callables and inputs must be picklable when workers > 1. Results are collected
    in submission order; a failed item raises, never returns partial success.
    """

    def __init__(self, workers: int) -> None:
        if not 1 <= workers <= 3:
            raise ValueError("regional workers must be between one and three")
        self.workers = workers
        self._executor: ProcessPoolExecutor | None = None
        self._open = False

    def __enter__(self) -> "OwnedProcessPool":
        if self._open:
            raise RuntimeError("parallel executor is already open")
        if self.workers > 1:
            self._executor = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=get_context("spawn"),
                initializer=_initialize_worker,
            )
        self._open = True
        return self

    def map(
        self, function: Callable[[Input], Output], values: Sequence[Input]
    ) -> list[Output]:
        if not self._open:
            raise RuntimeError("parallel executor is not open")
        if self._executor is None:
            if self.workers != 1:
                raise RuntimeError("parallel executor is not open")
            return [function(value) for value in values]
        futures = [
            self._executor.submit(_execute_isolated, function, value)
            for value in values
        ]
        try:
            return [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        self._open = False

"""One bounded periodic worker; exceptions stop it and remain observable."""

from collections.abc import Callable
from threading import Event, Thread
from time import monotonic


class PeriodicWorker:
    def __init__(self, callback: Callable[[], None], interval: float):
        self.callback, self.interval = callback, interval
        self.stop_event = Event()
        self.error: Exception | None = None
        self.last_completed = monotonic()
        self.thread = Thread(target=self._run, name="synthetic-telemetry", daemon=True)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.callback()
                self.last_completed = monotonic()
            except Exception as exc:
                self.error = exc
                return

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.ident is None:
            return
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("periodic worker did not stop")

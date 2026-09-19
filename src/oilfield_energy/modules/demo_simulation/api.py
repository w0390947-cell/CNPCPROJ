"""Public simulator construction API."""

from collections.abc import Callable
from threading import RLock

from .application import verify_demo
from .contracts import (
    AvailabilityPoint,
    Command,
    DeviceAvailabilitySeries,
    Fault,
    Frame,
    Receipt,
    ResourceAvailability,
)
from .domain import Fleet


class DemoService:
    def __init__(self, fleet: Fleet, journal: Callable[[str], None]):
        self.fleet, self.journal = fleet, journal
        self.lock = RLock()

    def tick(self) -> None:
        with self.lock:
            self.journal(self.fleet.step().model_dump_json())

    def snapshot(self) -> Frame:
        with self.lock:
            if self.fleet.frame is None:
                raise RuntimeError("no telemetry yet")
            return self.fleet.frame.model_copy(
                update={
                    "recovery_blocked": self.fleet.blocked,
                    "receipts": tuple(self.fleet.receipts.values())[-100:],
                }
            )

    def availability(self) -> ResourceAvailability:
        """Expose the fixed 24-hour renewable profiles through the domain port."""

        with self.lock:
            series: list[DeviceAvailabilitySeries] = []
            for device in self.fleet.bundle.devices:
                if device.kind not in ("wind", "pv"):
                    continue
                series.append(
                    DeviceAvailabilitySeries(
                        device_id=device.device_id,
                        region=device.region,
                        kind=device.kind,
                        points=tuple(
                            AvailabilityPoint(
                                minute_of_day=minute,
                                p_available_mw=self.fleet.network.available(
                                    device, minute
                                ),
                            )
                            for minute in range(0, 1440, 15)
                        ),
                    )
                )
            return ResourceAvailability(
                dataset_id=self.fleet.bundle.dataset_id,
                series=tuple(series),
            )

    def submit(self, command: Command) -> Receipt:
        with self.lock:
            receipt = self.fleet.submit(command)
            self.journal(receipt.model_dump_json())
            return receipt

    def fault(self, fault: Fault) -> None:
        with self.lock:
            self.fleet.fault = fault
            # Evaluate fault immediately so a command cannot slip in before next tick.
            self.tick()

    def rearm(self) -> bool:
        with self.lock:
            return self.fleet.rearm()


__all__ = ["DemoService", "Fleet", "verify_demo"]

"""Typed optimization-to-device resource schedule contracts.

The optimization models may coordinate aggregate P/Q at the PCC, but they
must not discard the identity, connection bus, or individual P/Q schedule of
the controllable resources that will later receive device-level commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

import numpy as np


class ResourceType(str, Enum):
    WIND = "wind"
    PV = "pv"
    STORAGE = "storage"
    SVG = "svg"


@dataclass(frozen=True)
class ResourceSchedule:
    """Immutable P/Q schedule for one optimization resource.

    A resource is identified independently from its bus because several field
    devices may share one connection point.  The current synthetic models use
    one optimization resource per wind/PV bus plus one storage and one SVG;
    future field case builders can preserve actual equipment identifiers.
    """

    resource_id: str
    bus_id: str
    resource_type: ResourceType
    active_power_mw: np.ndarray
    reactive_power_mvar: np.ndarray

    def __post_init__(self) -> None:
        for name in ("resource_id", "bus_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.resource_type, ResourceType):
            raise ValueError("resource_type must be a ResourceType")

        active = np.asarray(self.active_power_mw, dtype=float)
        reactive = np.asarray(self.reactive_power_mvar, dtype=float)
        if active.ndim != 1 or reactive.ndim != 1:
            raise ValueError("resource schedules must be one-dimensional")
        if active.size == 0 or active.shape != reactive.shape:
            raise ValueError(
                "resource active/reactive schedules must have the same nonzero length"
            )
        if not np.all(np.isfinite(active)) or not np.all(np.isfinite(reactive)):
            raise ValueError("resource schedules must contain only finite values")

        active = active.copy()
        reactive = reactive.copy()
        active.setflags(write=False)
        reactive.setflags(write=False)
        object.__setattr__(self, "active_power_mw", active)
        object.__setattr__(self, "reactive_power_mvar", reactive)

    @property
    def time_steps(self) -> int:
        return int(self.active_power_mw.size)


def validate_resource_schedules(
    schedules: Iterable[ResourceSchedule],
    *,
    expected_time_steps: int,
) -> tuple[ResourceSchedule, ...]:
    """Return a deterministic validated schedule tuple."""

    if not isinstance(expected_time_steps, int) or expected_time_steps <= 0:
        raise ValueError("expected_time_steps must be a positive integer")
    result = tuple(schedules)
    ids = [schedule.resource_id for schedule in result]
    if len(set(ids)) != len(ids):
        raise ValueError("resource schedule ids must be unique")
    if any(schedule.time_steps != expected_time_steps for schedule in result):
        raise ValueError("resource schedules must align with the optimization horizon")
    return result


__all__ = [
    "ResourceSchedule",
    "ResourceType",
    "validate_resource_schedules",
]

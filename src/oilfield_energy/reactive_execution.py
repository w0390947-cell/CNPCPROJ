"""Device-bounded reactive-power execution helpers.

This module never creates an aggregate fictitious Q actuator.  It derives the
instantaneous reactive envelope of every optimization resource and allocates a
requested aggregate value back to explicit resource setpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Iterable, Mapping

from .data import MicrogridData
from .resource_control_contracts import ResourceSchedule, ResourceType


_TOLERANCE = 1e-9


@dataclass(frozen=True)
class ReactiveCapability:
    """Known reactive interval for one resource in one execution snapshot."""

    resource_id: str
    minimum_mvar: float
    maximum_mvar: float

    def __post_init__(self) -> None:
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        if not isfinite(self.minimum_mvar) or not isfinite(self.maximum_mvar):
            raise ValueError("reactive capability bounds must be finite")
        if self.minimum_mvar > self.maximum_mvar:
            raise ValueError("minimum_mvar cannot exceed maximum_mvar")


@dataclass(frozen=True)
class ReactiveAllocation:
    """One deterministic, capability-bounded aggregate-Q allocation."""

    requested_total_mvar: float
    achieved_total_mvar: float
    unserved_mvar: float
    targets_mvar: tuple[tuple[str, float], ...]

    @property
    def target_by_resource(self) -> dict[str, float]:
        return dict(self.targets_mvar)


def calculate_reactive_capabilities(
    microgrid: MicrogridData,
    schedules: Iterable[ResourceSchedule],
    active_power_mw: Mapping[str, float],
) -> tuple[ReactiveCapability, ...]:
    """Calculate each resource envelope from its current active output.

    Wind uses the field ``|Q| <= kP`` contract when configured, otherwise its
    converter MVA circle. PV and storage require explicit reactive authority;
    SVG uses its configured signed limits.
    """

    schedules = tuple(schedules)
    active = dict(active_power_mw)
    schedule_ids = {item.resource_id for item in schedules}
    if set(active) != schedule_ids:
        raise ValueError("active-power snapshot must cover every resource exactly")

    capabilities: list[ReactiveCapability] = []
    for schedule in schedules:
        p = float(active[schedule.resource_id])
        if not isfinite(p) or p < -_TOLERANCE:
            raise ValueError("resource active power must be nonnegative and finite")
        p = max(0.0, p)
        if schedule.resource_type is ResourceType.WIND:
            apparent = float(microgrid.wind_capacity_mva[schedule.bus_id])
            circle_limit = sqrt(max(0.0, apparent * apparent - p * p))
            ratio = microgrid.wind_q_over_p_limit(schedule.bus_id)
            if ratio is not None:
                limit = min(circle_limit, max(0.0, float(ratio) * p))
            else:
                limit = circle_limit
            lower, upper = -limit, limit
        elif schedule.resource_type is ResourceType.PV:
            if microgrid.pv_can_control_reactive(schedule.bus_id):
                apparent = float(microgrid.pv_capacity_mva[schedule.bus_id])
                limit = sqrt(max(0.0, apparent * apparent - p * p))
            else:
                limit = 0.0
            lower, upper = -limit, limit
        elif schedule.resource_type is ResourceType.STORAGE:
            if microgrid.storage_reactive_enabled:
                apparent = float(microgrid.storage.s_max_mva)
                limit = sqrt(max(0.0, apparent * apparent - p * p))
            else:
                limit = 0.0
            lower, upper = -limit, limit
        elif schedule.resource_type is ResourceType.SVG:
            capability = microgrid.svg_capability()
            lower = capability.effective_q_min_mvar
            upper = capability.effective_q_max_mvar
        else:  # pragma: no cover - ResourceType makes this defensive only.
            raise ValueError(f"unsupported resource type: {schedule.resource_type}")
        capabilities.append(ReactiveCapability(schedule.resource_id, lower, upper))
    return tuple(capabilities)


def allocate_bounded_reactive_power(
    requested_total_mvar: float,
    base_targets_mvar: Mapping[str, float],
    capabilities: Iterable[ReactiveCapability],
) -> ReactiveAllocation:
    """Move from per-resource base targets to an aggregate target.

    The base plan is clipped to the current physical envelope first. Remaining
    movement is distributed in proportion to same-cycle headroom. A residual
    is returned explicitly when the requested total is outside the envelope.
    """

    requested = float(requested_total_mvar)
    if not isfinite(requested):
        raise ValueError("requested_total_mvar must be finite")
    capabilities = tuple(capabilities)
    if not capabilities:
        return ReactiveAllocation(requested, 0.0, requested, ())
    ids = [item.resource_id for item in capabilities]
    if len(set(ids)) != len(ids):
        raise ValueError("reactive capability resource ids must be unique")
    if set(base_targets_mvar) != set(ids):
        raise ValueError("base targets must cover every reactive capability exactly")

    lower = [item.minimum_mvar for item in capabilities]
    upper = [item.maximum_mvar for item in capabilities]
    targets = []
    for index, resource_id in enumerate(ids):
        value = float(base_targets_mvar[resource_id])
        if not isfinite(value):
            raise ValueError("reactive base targets must be finite")
        targets.append(min(upper[index], max(lower[index], value)))

    delta = requested - sum(targets)
    if delta > _TOLERANCE:
        headroom = [max(0.0, upper[i] - targets[i]) for i in range(len(ids))]
        available = sum(headroom)
        movement = min(delta, available)
        if available > _TOLERANCE:
            for index, room in enumerate(headroom):
                targets[index] += movement * room / available
    elif delta < -_TOLERANCE:
        headroom = [max(0.0, targets[i] - lower[i]) for i in range(len(ids))]
        available = sum(headroom)
        movement = min(-delta, available)
        if available > _TOLERANCE:
            for index, room in enumerate(headroom):
                targets[index] -= movement * room / available

    achieved = float(sum(targets))
    residual = requested - achieved
    if abs(residual) <= _TOLERANCE:
        residual = 0.0
    return ReactiveAllocation(
        requested_total_mvar=requested,
        achieved_total_mvar=achieved,
        unserved_mvar=residual,
        targets_mvar=tuple(zip(ids, targets, strict=True)),
    )


__all__ = [
    "ReactiveAllocation",
    "ReactiveCapability",
    "allocate_bounded_reactive_power",
    "calculate_reactive_capabilities",
]

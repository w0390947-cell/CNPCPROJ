"""对参数化算例应用可复现的预设事件，不修改原始算例对象。"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .data import MicrogridData, ProjectCase
from .modules.studies.contracts import EventTimeAxis, EventType, ScenarioEvent


def _scaled_profile(
    values: dict[str, np.ndarray],
    capacities: dict[str, float] | None,
    mask: np.ndarray,
    factor: float,
) -> dict[str, np.ndarray]:
    updated: dict[str, np.ndarray] = {}
    for name, original in values.items():
        profile = np.asarray(original, dtype=float).copy()
        profile[mask] *= factor
        if capacities is not None:
            profile = np.minimum(profile, capacities[name])
        updated[name] = profile
    return updated


def _apply_physical_event(
    case: ProjectCase,
    microgrid: MicrogridData,
    event: ScenarioEvent,
) -> MicrogridData:
    minute = np.asarray(case.time_hours, dtype=float) * 60.0
    mask = (minute >= event.start) & (minute < event.end)
    if not np.any(mask):
        return microgrid
    if event.event_type is EventType.PV_SURGE:
        return replace(microgrid, pv_available_mw=_scaled_profile(
            microgrid.pv_available_mw, microgrid.pv_capacity_mw, mask, event.magnitude,
        ))
    if event.event_type is EventType.WIND_SURGE:
        return replace(microgrid, wind_available_mw=_scaled_profile(
            microgrid.wind_available_mw, microgrid.wind_capacity_mw, mask, event.magnitude,
        ))
    if event.event_type in {EventType.LOAD_DROP, EventType.LOAD_SURGE}:
        return replace(
            microgrid,
            load_p_mw=_scaled_profile(microgrid.load_p_mw, None, mask, event.magnitude),
            load_q_mvar=_scaled_profile(microgrid.load_q_mvar, None, mask, event.magnitude),
        )
    return microgrid


def apply_physical_events(case: ProjectCase, events: list[ScenarioEvent]) -> ProjectCase:
    """返回应用物理时序事件后的算例副本；通信事件由协调层解释。"""
    physical_events = [
        event for event in events
        if event.event_type not in {
            EventType.COMMUNICATION_PACKET_LOSS,
            EventType.COMMUNICATION_OUTAGE,
        }
    ]
    if not physical_events:
        return case
    microgrids: list[MicrogridData] = []
    for microgrid in case.microgrids:
        updated = microgrid
        for event in physical_events:
            if event.target == microgrid.name:
                updated = _apply_physical_event(case, updated, event)
        microgrids.append(updated)
    return replace(case, microgrids=microgrids)


__all__ = [
    "EventTimeAxis", "EventType", "ScenarioEvent", "apply_physical_events",
]

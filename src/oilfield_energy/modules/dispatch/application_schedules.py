"""Pure assembly/slicing and archive validation for adopted rolling commands."""

import json
from datetime import datetime, timedelta

from .contracts import (
    AdoptedSchedule,
    FirstStepDecision,
    LegacyAdoptedScheduleRecord,
    ResourceTrajectory,
)


def adopt_first_steps(
    decisions: tuple[FirstStepDecision, ...],
    *,
    microgrid_id: str,
    start: datetime,
    step_minutes: int,
) -> AdoptedSchedule:
    if not decisions:
        raise ValueError("cannot adopt an empty rolling run")
    resources = decisions[0].resources
    expected = {r.resource_id: (r.bus_id, r.resource_type) for r in resources}
    by_step = [{r.resource_id: r for r in d.resources} for d in decisions]
    if any(
        {key: (r.bus_id, r.resource_type) for key, r in row.items()} != expected for row in by_step
    ):
        raise ValueError("rolling windows must preserve resource identity, bus and type")
    return AdoptedSchedule(
        microgrid_id=microgrid_id,
        start=start,
        step_minutes=step_minutes,
        p_grid_mw=tuple(d.p_grid_mw for d in decisions),
        q_grid_mvar=tuple(d.q_grid_mvar for d in decisions),
        wind_available_mw=tuple(d.wind_available_mw for d in decisions),
        origins=tuple(d.origin for d in decisions),
        resource_schedules=tuple(
            ResourceTrajectory(
                resource_id=r.resource_id,
                bus_id=r.bus_id,
                resource_type=r.resource_type,
                active_power_mw=tuple(row[r.resource_id].active_power_mw[0] for row in by_step),
                reactive_power_mvar=tuple(
                    row[r.resource_id].reactive_power_mvar[0] for row in by_step
                ),
            )
            for r in resources
        ),
    )


def slice_adopted_schedule(schedule: AdoptedSchedule, start: int, stop: int) -> AdoptedSchedule:
    if not 0 <= start < stop <= len(schedule.p_grid_mw):
        raise ValueError("invalid adopted schedule window")
    return AdoptedSchedule(
        microgrid_id=schedule.microgrid_id,
        start=schedule.start + timedelta(minutes=start * schedule.step_minutes),
        step_minutes=schedule.step_minutes,
        p_grid_mw=schedule.p_grid_mw[start:stop],
        q_grid_mvar=schedule.q_grid_mvar[start:stop],
        wind_available_mw=schedule.wind_available_mw[start:stop],
        origins=schedule.origins[start:stop],
        resource_schedules=tuple(
            ResourceTrajectory(
                resource_id=r.resource_id,
                bus_id=r.bus_id,
                resource_type=r.resource_type,
                active_power_mw=r.active_power_mw[start:stop],
                reactive_power_mvar=r.reactive_power_mvar[start:stop],
            )
            for r in schedule.resource_schedules
        ),
    )


def read_adopted_schedule(
    content: str,
) -> AdoptedSchedule | LegacyAdoptedScheduleRecord:
    payload: object = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("adopted schedule archive must be an object")
    if "schema_version" in payload:
        return AdoptedSchedule.model_validate_json(content)
    return LegacyAdoptedScheduleRecord.model_validate_json(content)

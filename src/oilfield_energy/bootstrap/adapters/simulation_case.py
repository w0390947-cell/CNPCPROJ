"""Typed projection to the existing optimization input; no synthetic fallback.

The legacy OptimizationResult stores untyped dictionaries. Any is confined to
this compatibility adapter and its sibling numerical runner, not new contracts.
"""
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from datetime import datetime, timedelta
from typing import Any, cast

import numpy as np

from oilfield_energy.data import (
    Line,
    MicrogridData,
    ModelAssumptions,
    ProjectCase,
    Storage,
)
from oilfield_energy.model import OptimizationResult
from oilfield_energy.modules.control.contracts import PlantInputs
from oilfield_energy.modules.dispatch.api import (
    adopt_first_steps,
    slice_adopted_schedule,
)
from oilfield_energy.modules.dispatch.contracts import (
    AdoptedOrigin,
    AdoptedSchedule,
    FirstStepDecision,
    ResourceTrajectory,
)
from oilfield_energy.modules.resources.contracts import ResourceIdentity
from oilfield_energy.modules.studies.contracts import StudySpec
from oilfield_energy.network_model import NetworkModelV2
from oilfield_energy.resource_control_contracts import ResourceSchedule


def make_case(
    spec: StudySpec,
    network: NetworkModelV2,
    profiles: PlantInputs,
    *,
    start: int = 0,
    stop: int | None = None,
    initial_energy: float | None = None,
) -> ProjectCase:
    stop = profiles.steps if stop is None else stop
    if profiles.step_minutes != 15 or not 0 <= start < stop <= profiles.steps:
        raise ValueError("optimization requires a valid 15-minute profile window")
    storage = next(r for r in spec.resources if r.kind == "storage")
    svg = next(r for r in spec.resources if r.kind == "svg")

    def arrays(name: str) -> dict[str, np.ndarray]:
        return {s.bus_id: np.array(s.values[start:stop]) for s in getattr(profiles, name)}

    def capacity(kind: str, apparent: bool = False) -> dict[str, float]:
        return {
            r.bus_id: r.s_max_mva if apparent else r.p_max_mw
            for r in spec.resources
            if r.kind == kind
        }

    mg = MicrogridData(
        name=spec.region_id,
        buses=list(spec.bus_ids),
        lines=[
            Line(b.branch_id, b.from_bus_id, b.to_bus_id, b.r_pu, b.x_pu, b.s_max_mva)
            for b in network.branches
        ],
        pcc_bus=network.pcc_bus_id,
        base_mva=network.base_mva,
        maximum_load_mw=sum(load.peak_mw for load in spec.loads),
        p_grid_max_mw=spec.pcc_max_mw,
        p_grid_min_mw=spec.pcc_min_mw,
        voltage_min_pu=spec.voltage_min_pu,
        voltage_max_pu=spec.voltage_max_pu,
        load_p_mw=arrays("load_p"),
        load_q_mvar=arrays("load_q"),
        wind_available_mw=arrays("wind_available"),
        wind_capacity_mw=capacity("wind"),
        wind_capacity_mva=capacity("wind", True),
        pv_available_mw=arrays("pv_available"),
        pv_capacity_mw=capacity("pv"),
        pv_capacity_mva=capacity("pv", True),
        storage=Storage(
            storage.bus_id,
            storage.p_max_mw,
            spec.storage.energy_mwh,
            spec.storage.minimum_mwh,
            spec.storage.initial_mwh if initial_energy is None else initial_energy,
            spec.storage.eta_charge,
            spec.storage.eta_discharge,
            storage.s_max_mva,
        ),
        svg_bus=svg.bus_id,
        svg_q_min_mvar=-svg.q_max_mvar,
        svg_q_max_mvar=svg.q_max_mvar,
        svg_s_max_mva=svg.s_max_mva,
        resource_identities=tuple(
            ResourceIdentity(r.resource_id, r.bus_id, r.kind) for r in spec.resources
        ),
        wind_q_abs_over_p_max={
            r.bus_id: min(0.328, r.q_max_mvar / r.p_max_mw)
            for r in spec.resources
            if r.kind == "wind"
        },
        pv_reactive_enabled={r.bus_id: False for r in spec.resources if r.kind == "pv"},
        storage_reactive_enabled=False,
        network_model_v2=network,
        network_operating_mode_id="normal",
    )
    hours = np.array(
        [
            (profiles.start + timedelta(minutes=i * 15)).hour
            + (profiles.start + timedelta(minutes=i * 15)).minute / 60
            for i in range(start, stop)
        ]
    )
    price = np.array([280.0 if h < 7 else 1180.0 if 17 <= h < 22 else 720.0 for h in hours])
    return ProjectCase(
        hours,
        price,
        [mg],
        ModelAssumptions(dt_hours=0.25, pf_min=spec.pf_min, no_reverse_margin_mw=spec.pcc_min_mw),
        spec.pcc_max_mw,
    )


def join_first_steps(
    results: list[OptimizationResult], *, start: datetime, step_minutes: int = 15
) -> AdoptedSchedule:
    """Translate legacy numerical outputs; assembly rules belong to dispatch."""
    if not results or any(not r.success for r in results):
        raise ValueError("cannot assemble an incomplete rolling schedule")
    decisions: list[FirstStepDecision] = []
    for index, result in enumerate(results):
        row = cast(dict[str, Any], result.microgrids["SC"])
        schedules: tuple[ResourceSchedule, ...] = row["resource_schedules"]
        decisions.append(
            FirstStepDecision(
                origin=AdoptedOrigin(
                    interval_index=index,
                    window_start_interval=index,
                    window_end_interval=index + len(row["p_grid_mw"]),
                ),
                p_grid_mw=float(row["p_grid_mw"][0]),
                q_grid_mvar=float(row["q_grid_mvar"][0]),
                wind_available_mw=float(row["wind_available_mw"][0]),
                resources=tuple(
                    ResourceTrajectory.model_validate(
                        dict(
                            resource_id=r.resource_id,
                            bus_id=r.bus_id,
                            resource_type=r.resource_type.value,
                            active_power_mw=(float(r.active_power_mw[0]),),
                            reactive_power_mvar=(float(r.reactive_power_mvar[0]),),
                        )
                    )
                    for r in schedules
                ),
            )
        )
    return adopt_first_steps(
        tuple(decisions), microgrid_id="SC", start=start, step_minutes=step_minutes
    )


def slice_schedule(result: AdoptedSchedule, start: int, stop: int) -> AdoptedSchedule:
    return slice_adopted_schedule(result, start, stop)

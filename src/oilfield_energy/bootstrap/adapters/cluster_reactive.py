"""Project physical Q facets and completed device states into dispatch rows."""

from collections.abc import Sequence
from dataclasses import replace
from math import cos, pi, sin

from oilfield_energy.data import ProjectCase
from oilfield_energy.modules.control.contracts import (
    DeviceCheckpoint,
    DynamicTrackingPolicy,
)
from oilfield_energy.modules.dispatch.api import plan_reactive_envelope
from oilfield_energy.modules.dispatch.contracts import (
    ReactivePlanningPolicy,
    ReactivePlanningResource,
)
from oilfield_energy.modules.resources.contracts import ResourceKind


def with_reactive_planning(
    case: ProjectCase,
    *,
    policy: ReactivePlanningPolicy,
    dynamics: DynamicTrackingPolicy,
    checkpoints: dict[str, DeviceCheckpoint],
    storage_enabled: bool,
) -> ProjectCase:
    plans = {}
    sides = case.assumptions.polygon_sides

    def polygon(capacity: float) -> tuple[tuple[float, float, float], ...]:
        return tuple(
            (
                cos(2 * pi * k / sides),
                sin(2 * pi * k / sides),
                capacity * cos(pi / sides),
            )
            for k in range(sides)
        )

    for mg in case.microgrids:
        initial = (
            checkpoints[mg.name].reactive_power_mvar if mg.name in checkpoints else {}
        )
        resources: list[ReactivePlanningResource] = []

        def add(
            kind: ResourceKind,
            bus: str,
            facets: Sequence[tuple[float, float, float]],
            controllable: bool,
        ) -> None:
            resource_id = mg.resource_id(kind, bus)
            if initial and resource_id not in initial:
                raise ValueError("reactive checkpoint must cover every resource")
            resources.append(
                ReactivePlanningResource(
                    resource_id, tuple(facets), initial.get(resource_id), controllable
                )
            )

        for bus in mg.wind_capacity_mw:
            capability = mg.wind_reactive_capability(bus)
            facets = list(polygon(capability.s_max_mva))
            facets.extend(
                (
                    (0.0, 1.0, capability.absolute_limit_mvar),
                    (0.0, -1.0, capability.absolute_limit_mvar),
                )
            )
            if capability.q_abs_over_p_max is not None:
                facets.extend(
                    (
                        (-capability.q_abs_over_p_max, 1.0, 0.0),
                        (-capability.q_abs_over_p_max, -1.0, 0.0),
                    )
                )
            add(
                "wind",
                bus,
                facets,
                capability.absolute_limit_mvar > 0 and capability.q_abs_over_p_max != 0,
            )
        for bus in mg.pv_capacity_mw:
            add(
                "pv",
                bus,
                polygon(mg.pv_capacity_mva[bus]),
                mg.pv_can_control_reactive(bus),
            )
        add(
            "storage",
            mg.storage.bus,
            polygon(mg.storage.s_max_mva),
            storage_enabled and mg.storage_reactive_enabled,
        )
        svg = mg.svg_capability()
        add(
            "svg",
            mg.svg_bus,
            (
                (0.0, 1.0, svg.effective_q_max_mvar),
                (0.0, -1.0, -svg.effective_q_min_mvar),
            ),
            svg.effective_q_max_mvar > svg.effective_q_min_mvar,
        )
        plans[mg.name] = plan_reactive_envelope(
            tuple(resources),
            steps=len(case.time_hours),
            step_minutes=case.assumptions.dt_hours * 60,
            time_constant_minutes=dynamics.time_constant_minutes,
            ramp_mvar_per_minute=dynamics.q_ramp_mvar_per_minute,
            policy=policy,
        )
    return replace(case, reactive_plans=plans)

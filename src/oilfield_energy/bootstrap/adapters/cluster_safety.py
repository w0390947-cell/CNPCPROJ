"""Project legacy studies into the sole certified cluster execution authority."""

from math import exp
from collections.abc import Mapping
from dataclasses import replace
import numpy as np
from typing import Literal
from oilfield_energy.modules.control.api import assess_dynamic_tracking
from oilfield_energy.data import ProjectCase
from oilfield_energy.modules.control.contracts import (
    TubeState,
    TubeCommand,
    DeviceCheckpoint,
    PlantInputs,
    DynamicTrackingAssessment,
)
from oilfield_energy.modules.power_flow.api import radial_point_response
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    RegionalExecution,
    ExecutionCheck,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    ClusterSafetyDomain,
    ClusterRegionDomain,
    ClusterForecast,
    ClusterSafetyEvidence,
)
from oilfield_energy.workflows.cluster_execution.api import (
    ClusterSafetyController,
    combined_status,
    bind_cluster_domain,
)
from .single_model import assemble_single_physical_model


def build_cluster_safety_domain(
    case: ProjectCase, policy: ExecutionPolicy, storage_enabled: bool
) -> ClusterSafetyDomain:
    research = policy.cluster_safety
    dynamics = policy.dynamic_tracking
    if research is None or dynamics is None:
        raise ValueError("cluster safety requires explicit research/response policies")
    total = round(len(case.time_hours) * case.assumptions.dt_hours * 60)
    regions = []
    for mg in case.microgrids:
        network, devices = assemble_single_physical_model(
            mg, case, policy, storage_enabled=storage_enabled
        )
        devices = devices.model_copy(
            update={
                "version": "cluster-device-v1",
                "resources": tuple(
                    r.model_copy(
                        update={"alpha_p": 1 - exp(-1 / dynamics.time_constant_minutes)}
                    )
                    if r.kind == "storage"
                    else r
                    for r in devices.resources
                ),
            }
        )
        forecasts = []
        scales = []
        for minute in range(total):
            k = min(
                len(case.time_hours) - 1, int(minute / (case.assumptions.dt_hours * 60))
            )
            loads = []
            for mapping in (mg.load_p_mw, mg.load_q_mvar):
                loads.append(
                    tuple(
                        PowerInterval(
                            low=float(mapping[b][k])
                            - abs(float(mapping[b][k])) * research.load_relative_error,
                            high=float(mapping[b][k])
                            + abs(float(mapping[b][k])) * research.load_relative_error,
                        )
                        for b in network.buses
                    )
                )
            availability = []
            scale = sum(
                float(v[k])
                for v in (
                    *mg.load_p_mw.values(),
                    *mg.wind_available_mw.values(),
                    *mg.pv_available_mw.values(),
                )
            )
            scales.append(scale)
            for r in devices.resources:
                series = (
                    mg.wind_available_mw if r.kind == "wind" else mg.pv_available_mw
                )
                error = (
                    research.wind_nameplate_error
                    if r.kind == "wind"
                    else research.pv_nameplate_error
                )
                availability.append(
                    max(0.0, float(series[r.bus][k]) - r.p.high * error)
                    if r.kind in ("wind", "pv")
                    else 0.0
                )
            forecasts.append(
                ClusterForecast(
                    load_p_mw=loads[0],
                    load_q_mvar=loads[1],
                    available_mw=tuple(availability),
                )
            )
        reserve = policy.storage_reserve if storage_enabled else None
        regions.append(
            ClusterRegionDomain(
                region=mg.name,
                network=network,
                devices=devices,
                forecasts=tuple(forecasts),
                initial=TubeState(
                    p_mw=tuple(0.0 for _ in devices.resources),
                    q_mvar=tuple(0.0 for _ in devices.resources),
                    energy_mwh=mg.storage.e_initial_mwh,
                ),
                required_reserve_mw=reserve.minimum_error_mw
                + reserve.forecast_error_fraction * max(scales)
                if reserve
                else 0.0,
                recovery_deadline_minute=reserve.recovery_minutes if reserve else 0,
            )
        )
    domain = ClusterSafetyDomain(
        policy=research,
        regions=tuple(regions),
        import_limit_mw=case.cluster_import_limit_mw,
        pf_target=policy.cluster_power_factor.target
        if policy.cluster_power_factor
        else case.assumptions.pf_dispatch_target,
        reserve_policy=policy.storage_reserve if storage_enabled else None,
        delivery_policy=policy.reserve_delivery if storage_enabled else None,
        context_id="pending",
    )
    return bind_cluster_domain(domain)


class ClusterSafetyPort:
    def __init__(
        self,
        case: ProjectCase,
        policy: ExecutionPolicy,
        storage_enabled: bool,
        plants: Mapping[str, PlantInputs],
        *,
        domain: ClusterSafetyDomain | None = None,
    ):
        self.domain = domain or build_cluster_safety_domain(
            case, policy, storage_enabled
        )
        self.controller = ClusterSafetyController(self.domain)
        self.plants = plants
        self.policy = policy
        self.plans: Mapping[str, RegionalExecution] = {}
        self.plan_start = 0
        self.target_plans: Mapping[str, RegionalExecution] = {}
        self.outputs: dict[
            str, list[tuple[float, float, float | None, float | None]]
        ] = {r.region: [] for r in self.domain.regions}

    def observed(self, minute: int) -> tuple[ClusterForecast, ...]:
        rows = []
        for r in self.domain.regions:
            plant = self.plants.get(r.region)
            if plant is None or not plant.load_p:
                raise ValueError("missing regional minute inputs")
            k = min(minute, len(plant.load_p[0].values) - 1)
            p = {x.bus_id: x.values[k] for x in plant.load_p}
            q = {x.bus_id: x.values[k] for x in plant.load_q}
            wind = {x.bus_id: x.values[k] for x in plant.wind_available}
            pv = {x.bus_id: x.values[k] for x in plant.pv_available}
            if (
                not set(r.network.buses) <= p.keys()
                or not set(r.network.buses) <= q.keys()
                or any(
                    x.bus not in (wind if x.kind == "wind" else pv)
                    for x in r.devices.resources
                    if x.kind in ("wind", "pv")
                )
            ):
                raise ValueError("minute input axes omit a modeled load or renewable")
            rows.append(
                ClusterForecast(
                    load_p_mw=tuple(
                        PowerInterval(low=p[b], high=p[b]) for b in r.network.buses
                    ),
                    load_q_mvar=tuple(
                        PowerInterval(low=q[b], high=q[b]) for b in r.network.buses
                    ),
                    available_mw=tuple(
                        (wind if x.kind == "wind" else pv)[x.bus]
                        if x.kind in ("wind", "pv")
                        else 0.0
                        for x in r.devices.resources
                    ),
                )
            )
        return tuple(rows)

    def planning_case(self, case: ProjectCase) -> ProjectCase:
        """Project the announced firm availability onto this planning window."""
        regions = {r.region: r for r in self.domain.regions}
        grids = []
        for mg in case.microgrids:
            r = regions[mg.name]
            available = {}
            for field, kind in (
                ("wind_available_mw", "wind"),
                ("pv_available_mw", "pv"),
            ):
                values = {}
                for bus, series in getattr(mg, field).items():
                    i = next(
                        i
                        for i, x in enumerate(r.devices.resources)
                        if x.kind == kind and x.bus == bus
                    )
                    values[bus] = np.array(
                        [
                            min(
                                float(v),
                                r.forecasts[
                                    min(round(t * 60), len(r.forecasts) - 1)
                                ].available_mw[i],
                            )
                            for t, v in zip(case.time_hours, series, strict=True)
                        ]
                    )
                available[field] = values
            grids.append(replace(mg, **available))
        return replace(case, microgrids=grids)

    def checkpoints(self) -> dict[str, DeviceCheckpoint]:
        minute = self.controller.minute
        observed = self.observed(max(0, minute - 1))
        return {
            r.region: DeviceCheckpoint(
                minute=minute,
                storage_energy_mwh=s.energy_mwh,
                storage_power_mw=s.p_mw[
                    next(
                        i
                        for i, x in enumerate(r.devices.resources)
                        if x.kind == "storage"
                    )
                ],
                active_power_mw={
                    x.resource_id: p
                    for x, p in zip(r.devices.resources, s.p_mw, strict=True)
                },
                reactive_power_mvar={
                    x.resource_id: q
                    for x, q in zip(r.devices.resources, s.q_mvar, strict=True)
                },
                gross_load_p_mw={
                    b: x.low for b, x in zip(r.network.buses, f.load_p_mw, strict=True)
                },
                gross_load_q_mvar={
                    b: x.low
                    for b, x in zip(r.network.buses, f.load_q_mvar, strict=True)
                },
            )
            for r, s, f in zip(
                self.domain.regions, self.controller.states, observed, strict=True
            )
        }

    def propose(self) -> tuple[TubeCommand, ...] | None:
        if not self.plans:
            return None
        commands = []
        minute = self.controller.minute
        for r, s in zip(self.domain.regions, self.controller.states, strict=True):
            plan = self.plans[r.region]
            times = plan.time_minutes
            index = max((i for i, t in enumerate(times) if t <= minute), default=0)
            p = tuple(
                plan.resource_p_mw[x.resource_id][index] for x in r.devices.resources
            )
            q = tuple(
                plan.resource_q_mvar[x.resource_id][index] for x in r.devices.resources
            )
            commands.append(TubeCommand(p_mw=p, q_mvar=q))
        return self.controller.commands_for_endpoints(tuple(commands))

    def advance(
        self, reason: str = "", *, reserve_requests: tuple[float, ...] | None = None
    ) -> None:
        minute = self.controller.minute
        observed = self.observed(minute)
        try:
            proposal = self.propose()
        except (ValueError, KeyError) as exc:
            proposal = None
            reason = str(exc)
        if self.plans:
            self.target_plans = self.plans
        step = self.controller.advance(
            observed, proposal, reason, reserve_requests=reserve_requests
        )
        for r, s, f in zip(self.domain.regions, step.after, observed, strict=True):
            p = [x.low for x in f.load_p_mw]
            q = [x.low for x in f.load_q_mvar]
            for resource, sp, sq in zip(
                r.devices.resources, s.p_mw, s.q_mvar, strict=True
            ):
                j = r.network.buses.index(resource.bus)
                p[j] -= sp
                q[j] -= sq
            pp, qq = radial_point_response(r.network, tuple(p), tuple(q))
            plan = self.target_plans.get(r.region)
            index = (
                max(
                    (i for i, t in enumerate(plan.time_minutes) if t <= minute),
                    default=0,
                )
                if plan
                else 0
            )
            pt, qt = (
                (plan.p_target_mw[index], plan.q_target_mvar[index])
                if plan
                else (None, None)
            )
            self.outputs[r.region].append((pp, qq, pt, qt))

    def minute_evidence(self, name: str, total: int) -> RegionalExecution:
        i = next(i for i, r in enumerate(self.domain.regions) if r.region == name)
        r = self.domain.regions[i]
        rows = self.outputs[name]
        steps = self.controller.steps
        complete = len(rows) == total
        fallback = any(s.source != "economic" for s in steps)
        dynamic: dict[Literal["p", "q"], DynamicTrackingAssessment] = {}
        if (
            rows
            and self.policy.dynamic_tracking is not None
            and all(x[2] is not None and x[3] is not None for x in rows)
        ):
            axes: tuple[
                tuple[Literal["p", "q"], int, int, Literal["MW", "Mvar"], float], ...
            ] = (
                ("p", 0, 2, "MW", self.policy.p_tracking_tolerance_mw),
                ("q", 1, 3, "Mvar", self.policy.q_tracking_tolerance_mvar),
            )
            for axis, actual_index, target_index, unit, limit in axes:
                dynamic[axis] = assess_dynamic_tracking(
                    time_minutes=tuple(float(s.minute) for s in steps),
                    target=tuple(v for x in rows if (v := x[target_index]) is not None),
                    actual=tuple(x[actual_index] for x in rows),
                    step_minutes=1,
                    unit=unit,
                    limit=limit,
                    numerical_tolerance=self.policy.numerical_tolerance,
                    policy=self.policy.dynamic_tracking,
                )
        checks = [
            ExecutionCheck(
                code="CLUSTER_INTERVAL_SAFETY",
                label="设备、能量及联合 AC 全区间安全",
                status="passed" if complete else "unknown",
                reason=self.controller.failure,
            ),
            ExecutionCheck(
                code="CLUSTER_CONTINUATION",
                label="已执行动作保留联合可行后续策略",
                status="passed" if complete else "unknown",
            ),
            ExecutionCheck(
                code="ECONOMIC_TRACKING",
                label="原经济 P/Q 目标跟踪",
                status="unknown"
                if fallback or not dynamic
                else combined_status(
                    [
                        s
                        for a in dynamic.values()
                        for s in (a.response_status, a.steady_status)
                    ]
                ),
                reason="认证后备允许偏离经济目标" if fallback else "",
            ),
        ]
        return RegionalExecution(
            region=name,
            status=combined_status([c.status for c in checks])
            if complete
            else "unknown",
            reason=self.controller.failure,
            checks=tuple(checks),
            dynamic_tracking=dynamic,
            time_minutes=tuple(float(s.minute) for s in steps),
            p_mw=tuple(x[0] for x in rows),
            q_mvar=tuple(x[1] for x in rows),
            p_target_mw=tuple(x[2] for x in rows if x[2] is not None)
            if all(x[2] is not None for x in rows)
            else (),
            q_target_mvar=tuple(x[3] for x in rows if x[3] is not None)
            if all(x[3] is not None for x in rows)
            else (),
            storage_power_mw=tuple(
                s.after[i].p_mw[
                    next(
                        j
                        for j, x in enumerate(r.devices.resources)
                        if x.kind == "storage"
                    )
                ]
                for s in steps
            ),
            storage_energy_mwh=tuple(s.after[i].energy_mwh for s in steps),
            resource_p_mw={
                x.resource_id: tuple(s.after[i].p_mw[j] for s in steps)
                for j, x in enumerate(r.devices.resources)
            },
            resource_q_mvar={
                x.resource_id: tuple(s.after[i].q_mvar[j] for s in steps)
                for j, x in enumerate(r.devices.resources)
            },
        )

    def evidence(self) -> ClusterSafetyEvidence:
        return self.controller.evidence()

"""Numerical and legacy-data ports for the sole formal single-microgrid workflow."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
from numpy.typing import NDArray

from oilfield_energy.data import ProjectCase
from oilfield_energy.model import OptimizationResult
from oilfield_energy.modules.control.contracts import (
    BusSeries,
    DeviceCheckpoint,
    PlantInputs,
    TubeCommand,
    TubeState,
)
from oilfield_energy.modules.dispatch.api import (
    single_planning_power_factor,
    storage_window_target,
)
from oilfield_energy.modules.dispatch.contracts import (
    CostRates,
    PQPlanningInput,
    PQPlanningWitness,
)
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from oilfield_energy.modules.studies.contracts import ScenarioEvent
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    RollingProgress,
)
from oilfield_energy.workflows.single_execution.api import (
    build_unified_domain,
    device_model_at,
    run_unified_feedback,
)
from oilfield_energy.workflows.single_execution.contracts import (
    BoundedBusLoad,
    FirmGeneration,
    SingleBoundaryRequest,
    SingleEconomicProposal,
    SingleExecution,
    SingleForecastEnvelope,
    SingleResearchPolicy,
    UnifiedDomain,
    TerminalEnergyTask,
)

from .cluster_execution import (
    RegionalPlanRequest,
    execution_inputs,
    realize_regional_plan,
)
from .cluster_rolling import forecast_grid, window_case
from .project_dataset import build_synthetic_case
from .single_model import assemble_single_physical_model
from .single_planning import prepare_single_case


def _checkpoint(domain: UnifiedDomain, state: TubeState, step: int) -> DeviceCheckpoint:
    i = next(i for i, r in enumerate(domain.devices.resources) if r.kind == "storage")
    return DeviceCheckpoint(
        minute=step,
        storage_energy_mwh=state.energy_mwh,
        storage_power_mw=state.p_mw[i],
        active_power_mw={
            r.resource_id: p
            for r, p in zip(domain.devices.resources, state.p_mw, strict=True)
        },
        reactive_power_mvar={
            r.resource_id: q
            for r, q in zip(domain.devices.resources, state.q_mvar, strict=True)
        },
    )


class SingleNumericalPorts:
    def __init__(
        self,
        case: ProjectCase,
        reference: OptimizationResult,
        region: str,
        domain: UnifiedDomain,
        plant: PlantInputs,
        policy: ExecutionPolicy,
        storage_enabled: bool,
        seconds: float,
        reference_minutes: float,
        declared: SingleBoundaryRequest | None,
    ) -> None:
        self.case, self.reference, self.region, self.domain, self.plant = (
            case,
            reference,
            region,
            domain,
            plant,
        )
        self.policy, self.storage_enabled, self.seconds = (
            policy,
            storage_enabled,
            seconds,
        )
        self.reference_minutes, self.declared = reference_minutes, declared

    def observe(self, step: int) -> SingleForecastEnvelope:
        mg = self.case.microgrids[0]
        q = {b.bus_id: b.values[step] for b in self.plant.load_q}
        return SingleForecastEnvelope(
            loads=tuple(
                BoundedBusLoad(
                    bus=b.bus_id,
                    p_mw=PowerInterval(low=b.values[step], high=b.values[step]),
                    q_mvar=PowerInterval(low=q[b.bus_id], high=q[b.bus_id]),
                )
                for b in self.plant.load_p
            ),
            generation=tuple(
                FirmGeneration(
                    resource_id=mg.resource_id(
                        "wind" if kind == "wind" else "pv", b.bus_id
                    ),
                    minimum_available_mw=b.values[step],
                )
                for kind, rows in (
                    ("wind", self.plant.wind_available),
                    ("pv", self.plant.pv_available),
                )
                for b in rows
            ),
        )

    def propose(self, step: int, state: TubeState) -> SingleEconomicProposal:
        interval = round(self.case.assumptions.dt_hours * 60)
        reactive = self.policy.reactive_planning
        if reactive is None:
            raise ValueError(
                "joint P/Q planning requires an explicit transition policy"
            )
        transition_minutes = min(interval, reactive.transition_minutes)
        if int(transition_minutes) != transition_minutes:
            raise ValueError(
                "joint P/Q transition must contain whole execution minutes"
            )
        stop = min(
            step + self.policy.horizon_minutes, len(self.case.time_hours) * interval
        )
        energy = np.asarray(
            self.reference.microgrids[self.region]["storage_energy_mwh"], dtype=float
        )
        terminal = (
            float(
                np.interp(stop, np.arange(len(energy)) * self.reference_minutes, energy)
            )
            if self.storage_enabled
            else state.energy_mwh
        )
        storage = self.case.microgrids[0].storage
        if (
            not storage.e_min_mwh - self.policy.numerical_tolerance
            <= terminal
            <= storage.e_max_mwh + self.policy.numerical_tolerance
        ):
            raise ValueError("reference terminal energy exceeds physical bounds")
        terminal = min(storage.e_max_mwh, max(storage.e_min_mwh, terminal))
        window = window_case(
            self.case,
            step // interval,
            stop // interval,
            {self.region: state.energy_mwh},
            {self.region: terminal},
        )
        task = self.domain.energy_task
        if task is not None and self.storage_enabled:
            window = replace(
                window,
                storage_window_targets={
                    self.region: storage_window_target(
                        minimum_mwh=storage.e_min_mwh,
                        maximum_mwh=storage.e_max_mwh,
                        task_minimum_mwh=task.energy_mwh.low,
                        task_maximum_mwh=task.energy_mwh.high,
                        remaining_hours=max(0, task.deadline_step - stop) / 60,
                        charge_limit_mw=storage.p_max_mw,
                        discharge_limit_mw=storage.p_max_mw,
                        eta_charge=storage.eta_charge,
                        eta_discharge=storage.eta_discharge,
                        reference_mwh=terminal,
                        reference_cost_cny_per_mwh=self.domain.research.energy_reference_cost_cny_per_mwh,
                    )
                },
            )
        mg = window.microgrids[0]
        generation = [
            {
                x.resource_id: x.minimum_available_mw
                for x in self.domain.forecasts[
                    min(k, len(self.domain.forecasts) - 1)
                ].generation
            }
            for k in range(step, stop, interval)
        ]
        mg = replace(
            mg,
            wind_available_mw={
                b: np.asarray([v[mg.resource_id("wind", b)] for v in generation])
                for b in mg.wind_capacity_mw
            },
            pv_available_mw={
                b: np.asarray([v[mg.resource_id("pv", b)] for v in generation])
                for b in mg.pv_capacity_mw
            },
        )
        base_pf_target = window.assumptions.pf_dispatch_target
        window = replace(
            window,
            microgrids=[mg],
            assumptions=replace(
                window.assumptions,
                pf_dispatch_target=max(
                    window.assumptions.pf_dispatch_target,
                    single_planning_power_factor(
                        window.assumptions.pf_min,
                        self.domain.research.planning_pf_tangent_fraction,
                    ),
                ),
            ),
        )
        window = prepare_single_case(
            window,
            self.region,
            storage_enabled=self.storage_enabled,
            policy=self.policy,
            checkpoint=_checkpoint(self.domain, state, step),
            joint_pq=PQPlanningInput(
                start_step=step,
                devices=self.domain.devices,
                initial=state,
                planning_steps=len(window.time_hours),
                planning_minutes=interval,
                transition_minutes=int(transition_minutes),
                p_ceiling_mw=tuple(
                    tuple(r.p.high for r in device_model_at(self.domain, k).resources)
                    for k in range(step, stop + 1)
                ),
                polygon_sides=window.assumptions.polygon_sides,
                reserve_fraction=reactive.reserve_fraction,
            ),
        )
        p_target = (
            np.full(len(window.time_hours), self.declared.target_p_mw)
            if self.declared
            else None
        )
        q_target = (
            np.full(len(window.time_hours), self.declared.target_q_mvar)
            if self.declared
            else None
        )
        result, solved = realize_regional_plan(
            RegionalPlanRequest(
                window,
                self.region,
                p_target,
                q_target,
                self.storage_enabled,
                self.seconds,
                self.policy,
            )
        )
        pf_targets = (window.assumptions.pf_dispatch_target,)
        # Extra proposal headroom is a preference, never an extra physical limit.
        # A failed tighter solve may retry the declared planning target; both
        # candidates still face the identical independent safety supervisor.
        if result.status != "passed" and base_pf_target < pf_targets[0]:
            window = replace(
                window,
                assumptions=replace(
                    window.assumptions, pf_dispatch_target=base_pf_target
                ),
            )
            result, solved = realize_regional_plan(
                RegionalPlanRequest(
                    window,
                    self.region,
                    p_target,
                    q_target,
                    self.storage_enabled,
                    self.seconds,
                    self.policy,
                )
            )
            pf_targets = (*pf_targets, base_pf_target)
        command = None
        if result.status == "passed" and solved is not None:
            command = TubeCommand(
                p_mw=tuple(
                    result.resource_p_mw[r.resource_id][0]
                    for r in self.domain.devices.resources
                ),
                q_mvar=tuple(
                    result.resource_q_mvar[r.resource_id][0]
                    for r in self.domain.devices.resources
                ),
            )
        return SingleEconomicProposal(
            start_step=step,
            initial=state,
            command=command,
            target_p_mw=self.declared.target_p_mw
            if self.declared
            else (result.p_mw[0] if result.p_mw else 0),
            target_q_mvar=self.declared.target_q_mvar
            if self.declared
            else (result.q_mvar[0] if result.q_mvar else 0),
            result=result,
            planning_pf_targets=pf_targets,
            pq_witness=PQPlanningWitness.model_validate(
                solved.microgrids[self.region]["joint_pq_witness"]
            )
            if solved is not None and result.status == "passed"
            else None,
            target_origin="external" if self.declared else "economic",
        )


def run_unified_single(
    case: ProjectCase,
    reference: OptimizationResult,
    region: str,
    *,
    storage_enabled: bool,
    time_limit_seconds: float,
    packaged_inputs: bool,
    events: list[ScenarioEvent],
    policy: ExecutionPolicy,
    progress: Callable[[RollingProgress], None] | None = None,
    boundary: SingleBoundaryRequest | None = None,
    research: SingleResearchPolicy | None = None,
) -> SingleExecution:
    research = research or SingleResearchPolicy(
        forecast_interpolation="linear" if packaged_inputs else "hold"
    )
    source = build_synthetic_case(steps=96) if packaged_inputs else case
    if packaged_inputs and source.dataset_sha256 != case.dataset_sha256:
        raise ValueError(
            "native forecast dataset identity differs from the reference study"
        )
    intraday, plants, basis = execution_inputs(
        source, packaged=packaged_inputs, events=events
    )
    intraday = replace(
        intraday,
        microgrids=[m for m in intraday.microgrids if m.name == region],
        reactive_plans={},
        storage_reserves={},
    )
    intraday = forecast_grid(intraday, policy.update_minutes)
    mg = intraday.microgrids[0]

    def series(rows: dict[str, NDArray[np.float64]]) -> tuple[BusSeries, ...]:
        return tuple(
            BusSeries(bus_id=b, values=tuple(float(v) for v in values))
            for b, values in rows.items()
        )

    forecast = PlantInputs(
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        step_minutes=policy.update_minutes,
        load_p=series(mg.load_p_mw),
        load_q=series(mg.load_q_mvar),
        wind_available=series(mg.wind_available_mw),
        pv_available=series(mg.pv_available_mw),
    )
    network, devices = assemble_single_physical_model(
        mg, intraday, policy, storage_enabled=storage_enabled
    )
    assert policy.dynamic_tracking is not None
    total = round(len(case.time_hours) * case.assumptions.dt_hours * 60)
    domain = build_unified_domain(
        network,
        devices,
        forecast,
        research,
        total_steps=total,
        p_band_mw=policy.p_tracking_tolerance_mw,
        q_band_mvar=policy.q_tracking_tolerance_mvar,
        response_steps=policy.dynamic_tracking.maximum_response_minutes,
        declared=boundary,
        energy_task=TerminalEnergyTask(
            task_id=f"{region}:terminal-storage-energy",
            deadline_step=total,
            energy_mwh=PowerInterval(
                low=max(
                    mg.storage.e_min_mwh,
                    mg.storage.terminal_energy_mwh
                    - case.assumptions.terminal_energy_tolerance_mwh,
                ),
                high=min(
                    mg.storage.e_max_mwh,
                    mg.storage.terminal_energy_mwh
                    + case.assumptions.terminal_energy_tolerance_mwh,
                ),
            ),
            source="Original case Storage.terminal_energy_mwh and terminal_energy_tolerance_mwh; absolute study end",
        )
        if storage_enabled
        else None,
        economic_prices_cny_per_mwh=tuple(
            float(
                intraday.price_cny_per_mwh[
                    min(k // policy.update_minutes, len(intraday.time_hours) - 1)
                ]
            )
            for k in range(total)
        ),
        economic_rates=CostRates(
            curtailment_cny_per_mwh=case.assumptions.curtailment_cost_cny_per_mwh,
            storage_throughput_cny_per_mwh=case.assumptions.storage_degradation_cny_per_mwh,
            loss_cny_per_mwh=case.assumptions.loss_value_cny_per_mwh,
        ),
    )
    initial = TubeState(
        p_mw=domain.terminal_command.p_mw,
        q_mvar=domain.terminal_command.q_mvar,
        energy_mwh=mg.storage.e_initial_mwh,
    )
    if boundary is not None and boundary.initial_state is not None:
        ids = tuple(r.resource_id for r in domain.devices.resources)
        if set(ids) != set(boundary.initial_resource_ids):
            raise ValueError("declared initial resource identities differ")
        axis = tuple(boundary.initial_resource_ids.index(r) for r in ids)
        initial = TubeState(
            p_mw=tuple(boundary.initial_state.p_mw[i] for i in axis),
            q_mvar=tuple(boundary.initial_state.q_mvar[i] for i in axis),
            energy_mwh=boundary.initial_state.energy_mwh,
        )
    ports = SingleNumericalPorts(
        intraday,
        reference,
        region,
        domain,
        plants[region],
        policy,
        storage_enabled,
        time_limit_seconds,
        case.assumptions.dt_hours * 60,
        boundary,
    )

    def report(completed: int) -> None:
        if progress is not None and completed % policy.update_minutes == 0:
            progress(
                RollingProgress(
                    current_window=(completed - 1) // policy.update_minutes + 1,
                    completed_windows=completed // policy.update_minutes,
                    total_windows=(total + policy.update_minutes - 1)
                    // policy.update_minutes,
                    start_minute=(completed - 1)
                    // policy.update_minutes
                    * policy.update_minutes,
                    end_minute=completed,
                    total_minutes=total,
                    phase="completed",
                )
            )

    execution = run_unified_feedback(
        domain,
        initial,
        total_steps=total,
        update_steps=policy.update_minutes,
        initial_target=(boundary.target_p_mw, boundary.target_q_mvar)
        if boundary
        else (0, 0),
        observe=ports.observe,
        propose=ports.propose,
        progress=report,
    )
    last = execution.steps[-1].after if execution.steps else initial
    return SingleExecution(
        version="single-execution-v9",
        region=region,
        status="passed"
        if execution.status == "passed"
        and execution.tracking_status == "passed"
        and execution.target_admission_status == "passed"
        and execution.hard_task.guarantee in ("maintained", "not_required")
        and execution.hard_task.outcome in ("met", "not_required")
        else "violated"
        if execution.tracking_status == "violated"
        or execution.target_admission_status == "rejected"
        else "unknown",
        reason=execution.reason
        or execution.hard_task.reason
        or (
            "one or more requested targets were not certified"
            if any(not a.accepted for a in execution.target_admissions)
            else ""
        )
        or (
            "PCC tracking did not meet every retained deadline"
            if execution.tracking_status == "violated"
            else ""
        ),
        storage_enabled=storage_enabled,
        policy=policy,
        input_basis=basis,
        initialization="declared_actual_state"
        if boundary and boundary.initial_state
        else "declared_safe_state",
        initial_state=_checkpoint(domain, initial, 0),
        last_completed_state=_checkpoint(domain, last, len(execution.steps)),
        total_minutes=total,
        completed_minutes=len(execution.steps),
        unified=execution,
    )

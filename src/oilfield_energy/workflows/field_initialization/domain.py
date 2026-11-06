"""Map explicitly bounded field inputs into the existing single-grid contracts."""

from bisect import bisect_right
from datetime import timedelta

from oilfield_energy.modules.control.contracts import (
    TubeCommand,
    TubeDeviceModel,
    TubeResource,
    TubeState,
)
from oilfield_energy.modules.dispatch.contracts import CostRates
from oilfield_energy.modules.power_flow.contracts import (
    PowerInterval as I,
)
from oilfield_energy.modules.power_flow.contracts import (
    RadialEnvelope,
)
from oilfield_energy.workflows.single_execution.contracts import (
    BoundedBusLoad,
    FirmGeneration,
    SingleForecastEnvelope,
    SingleResearchPolicy,
    TerminalEnergyTask,
    UnifiedDomain,
)

from .contracts import ControlPolicy, InitializeRequest, ModelPackage
from .contracts_planning import ForecastLoad, ForecastRenewable, PlanningInputs
from .contracts_values import MeasuredValue
from .validation import require, unique


def build_domain(
    request: InitializeRequest,
    network: RadialEnvelope,
    context: str,
    *,
    completed_energy_task: bool = False,
) -> tuple[UnifiedDomain, TubeState]:
    p = request.payload
    require(
        p.initial_snapshot.snapshot_at == p.planning_inputs.effective_from,
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Initial state and planning origin differ",
    )
    domain = map_planning_domain(
        p.model_package,
        p.planning_inputs.model_copy(update={"energy_task": None})
        if completed_energy_task
        else p.planning_inputs,
        p.control_policy,
        network,
        context,
    )
    states = {x.resource_id: x for x in p.initial_snapshot.device_states}

    def number(value: MeasuredValue | None) -> float:
        assert value is not None and value.value is not None
        return value.value

    storage = next(d for d in p.model_package.devices if d.kind == "storage")
    initial = TubeState(
        p_mw=tuple(number(states[r.resource_id].p_mw) for r in domain.devices.resources),
        q_mvar=tuple(number(states[r.resource_id].q_mvar) for r in domain.devices.resources),
        energy_mwh=number(states[storage.resource_id].energy_mwh),
    )
    return domain, initial


def map_planning_domain(
    model_package: ModelPackage,
    plan: PlanningInputs,
    policy: ControlPolicy,
    network: RadialEnvelope,
    context: str,
) -> UnifiedDomain:
    start = plan.effective_from
    seconds = (plan.valid_until - start).total_seconds()
    require(
        plan.issued_at <= start
        and start.second == 0
        and start.microsecond == 0
        and 0 < seconds <= 86400
        and seconds % 60 == 0,
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Initialization requires an explicit aligned 1..1440 minute forecast clock",
    )
    require(
        seconds >= max(5400, policy.planning_horizon_seconds, policy.response_deadline_seconds),
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Coverage must include the planning horizon, response deadline and 90-minute backup construction",
    )
    points = plan.forecast_points
    dates = [x.at for x in points]
    require(
        dates == sorted(set(dates)) and dates[0] == start and dates[-1] == plan.valid_until,
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Forecast must have unique ordered endpoints exactly covering the declared interval",
    )
    require(
        all((x - start).total_seconds() % 60 == 0 for x in dates),
        "INVALID_MESSAGE",
        "Forecast points must lie on minute boundaries",
    )
    devices = {d.resource_id: d for d in model_package.devices}
    renewable = {k for k, d in devices.items() if d.kind in ("wind", "pv")}
    loads: list[dict[str, ForecastLoad]] = []
    generation: list[dict[str, ForecastRenewable]] = []
    for point in points:
        require(
            unique((x.bus_id for x in point.bus_loads), "forecast buses") == set(network.buses),
            "IDENTITY_MISMATCH",
            "Forecast must cover every bus",
        )
        require(
            unique((x.resource_id for x in point.renewables), "forecast resources") == renewable,
            "IDENTITY_MISMATCH",
            "Forecast renewable identities differ",
        )
        require(
            all(
                x.available_forecast_mw <= devices[x.resource_id].p_max_mw for x in point.renewables
            ),
            "INVALID_MESSAGE",
            "Renewable forecast exceeds nameplate",
        )
        loads.append({x.bus_id: x for x in point.bus_loads})
        generation.append({x.resource_id: x for x in point.renewables})
    tariffs = plan.tariff_intervals
    require(
        tariffs[0].start_at == start
        and tariffs[-1].end_at == plan.valid_until
        and all(x.start_at < x.end_at for x in tariffs)
        and all(a.end_at == b.start_at for a, b in zip(tariffs, tariffs[1:])),
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Tariffs must cover the full clock without gaps or overlaps",
    )
    forecast: list[SingleForecastEnvelope] = []
    prices: list[float] = []
    for step in range(int(seconds // 60) + 1):
        at = start + timedelta(minutes=step)
        k = min(bisect_right(dates, at) - 1, len(dates) - 2)
        f = (
            (at - dates[k]).total_seconds() / (dates[k + 1] - dates[k]).total_seconds()
            if plan.forecast_interpolation == "linear"
            else (1.0 if at == dates[-1] else 0.0)
        )

        def mix(a: float, b: float) -> float:
            return (1 - f) * a + f * b

        forecast.append(
            SingleForecastEnvelope(
                loads=tuple(
                    BoundedBusLoad(
                        bus=bus,
                        p_mw=I(
                            low=mix(
                                loads[k][bus].p_bounds_mw.low,
                                loads[k + 1][bus].p_bounds_mw.low,
                            ),
                            high=mix(
                                loads[k][bus].p_bounds_mw.high,
                                loads[k + 1][bus].p_bounds_mw.high,
                            ),
                        ),
                        q_mvar=I(
                            low=mix(
                                loads[k][bus].q_bounds_mvar.low,
                                loads[k + 1][bus].q_bounds_mvar.low,
                            ),
                            high=mix(
                                loads[k][bus].q_bounds_mvar.high,
                                loads[k + 1][bus].q_bounds_mvar.high,
                            ),
                        ),
                    )
                    for bus in network.buses
                ),
                generation=tuple(
                    FirmGeneration(
                        resource_id=rid,
                        minimum_available_mw=mix(
                            generation[k][rid].minimum_available_mw,
                            generation[k + 1][rid].minimum_available_mw,
                        ),
                    )
                    for rid in sorted(renewable)
                ),
            )
        )
        tariff = next((x for x in tariffs if x.start_at <= at < x.end_at), tariffs[-1])
        prices.append(tariff.import_price_cny_per_mwh)
    resources = tuple(
        TubeResource(
            resource_id=d.resource_id,
            bus=d.bus_id,
            kind=d.kind,
            p=I(low=d.p_min_mw, high=d.p_max_mw),
            q=I(low=d.q_min_mvar, high=d.q_max_mvar),
            s_mva=d.s_max_mva,
            q_over_p=d.q_abs_over_p_max,
            alpha_p=d.alpha_p,
            ramp_p_mw=d.p_change_limit_mw_per_cycle,
        )
        for d in model_package.devices
    )
    storage = next(d for d in devices.values() if d.kind == "storage")
    assert storage.energy_min_mwh is not None and storage.energy_max_mwh is not None
    assert storage.eta_charge is not None and storage.eta_discharge is not None
    model = TubeDeviceModel(
        resources=resources,
        alpha_q=policy.alpha_q,
        aggregate_q_ramp_mvar=policy.q_change_limit_mvar_per_cycle,
        step_minutes=1,
        energy_mwh=I(low=storage.energy_min_mwh, high=storage.energy_max_mwh),
        eta_charge=storage.eta_charge,
        eta_discharge=storage.eta_discharge,
        context_id=context,
        settling_resolution=policy.settling_resolution,
    )
    task = None
    if plan.energy_task is not None:
        t = plan.energy_task
        offset = (t.deadline_at - start).total_seconds()
        require(
            0 < offset <= seconds and offset % 60 == 0,
            "INVALID_MESSAGE",
            "Energy task deadline must be covered and minute aligned",
        )
        require(
            storage.energy_min_mwh
            <= t.energy_min_mwh
            <= t.energy_max_mwh
            <= storage.energy_max_mwh,
            "INVALID_MESSAGE",
            "Task energy lies outside storage capacity",
        )
        task = TerminalEnergyTask(
            task_id=t.task_id,
            deadline_step=int(offset // 60),
            energy_mwh=I(low=t.energy_min_mwh, high=t.energy_max_mwh),
            source=t.source_ref,
        )
    net_q = sum(sum((b.q_mvar.low + b.q_mvar.high) / 2 for b in f.loads) for f in forecast) / len(
        forecast
    ) - sum(network.shunt_q_mvar)
    svgs = [r for r in resources if r.kind == "svg"]
    terminal = TubeCommand(
        p_mw=tuple(0.0 for _ in resources),
        q_mvar=tuple(
            min(r.q.high, max(r.q.low, net_q / max(1, len(svgs)))) if r.kind == "svg" else 0.0
            for r in resources
        ),
    )
    rates = plan.cost_rates
    domain = UnifiedDomain(
        network=network,
        devices=model,
        context_id=context,
        research=SingleResearchPolicy(
            settling_resolution=policy.settling_resolution,
            forecast_interpolation=plan.forecast_interpolation,
        ),
        bound_source=plan.source_ref,
        forecasts=tuple(forecast),
        terminal_command=terminal,
        p_band_mw=policy.p_tracking_tolerance_mw,
        q_band_mvar=policy.q_tracking_tolerance_mvar,
        response_steps=policy.response_deadline_seconds // 60,
        energy_task=task,
        economic_prices_cny_per_mwh=tuple(prices),
        economic_rates=CostRates(
            curtailment_cny_per_mwh=rates.curtailment_cny_per_mwh,
            storage_throughput_cny_per_mwh=rates.storage_throughput_cny_per_mwh,
            loss_cny_per_mwh=rates.loss_cny_per_mwh,
        ),
    )
    return domain

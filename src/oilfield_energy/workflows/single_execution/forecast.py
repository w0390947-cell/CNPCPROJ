"""Causal research domains built from forecasts, never from future observations."""

from hashlib import sha256

from oilfield_energy.modules.control.contracts import (
    PlantInputs,
    TubeCommand,
    TubeDeviceModel,
)
from oilfield_energy.modules.dispatch.contracts import CostRates
from oilfield_energy.modules.power_flow.contracts import PowerInterval, RadialEnvelope

from .contracts_domain import BoundedBusLoad, FirmGeneration, SingleBoundaryRequest
from .contracts_tasks import TerminalEnergyTask
from .contracts_unified import (
    SingleForecastEnvelope,
    SingleResearchPolicy,
    UnifiedDomain,
)


def build_unified_domain(
    network: RadialEnvelope,
    devices: TubeDeviceModel,
    forecast: PlantInputs,
    research: SingleResearchPolicy,
    *,
    total_steps: int,
    p_band_mw: float,
    q_band_mvar: float,
    response_steps: int,
    declared: SingleBoundaryRequest | None = None,
    energy_task: TerminalEnergyTask | None = None,
    economic_prices_cny_per_mwh: tuple[float, ...] = (),
    economic_rates: CostRates | None = None,
) -> UnifiedDomain:
    p = {x.bus_id: x.values for x in forecast.load_p}
    q = {x.bus_id: x.values for x in forecast.load_q}
    wind = {x.bus_id: x.values for x in forecast.wind_available}
    pv = {x.bus_id: x.values for x in forecast.pv_available}
    envelopes: list[SingleForecastEnvelope] = []
    for step in range(total_steps):
        k = min(step // forecast.step_minutes, len(next(iter(p.values()))) - 1)

        def value(series: tuple[float, ...]) -> float:
            fraction = (
                (step % forecast.step_minutes) / forecast.step_minutes
                if research.forecast_interpolation == "linear"
                else 0.0
            )
            return (1 - fraction) * series[k] + fraction * series[
                min(k + 1, len(series) - 1)
            ]

        loads = tuple(
            BoundedBusLoad(
                bus=bus,
                p_mw=PowerInterval(
                    low=max(0, value(p[bus]) * (1 - research.load_relative_error)),
                    high=value(p[bus]) * (1 + research.load_relative_error),
                ),
                q_mvar=PowerInterval(
                    low=value(q[bus])
                    - abs(value(q[bus])) * research.load_relative_error,
                    high=value(q[bus])
                    + abs(value(q[bus])) * research.load_relative_error,
                ),
            )
            for bus in network.buses
        )
        generation = tuple(
            FirmGeneration(
                resource_id=r.resource_id,
                minimum_available_mw=max(
                    0,
                    value((wind if r.kind == "wind" else pv)[r.bus])
                    - r.p.high
                    * (
                        research.wind_nameplate_error
                        if r.kind == "wind"
                        else research.pv_nameplate_error
                    ),
                ),
            )
            for r in devices.resources
            if r.kind in ("wind", "pv")
        )
        envelopes.append(
            SingleForecastEnvelope(
                loads=declared.loads if declared else loads,
                generation=declared.generation if declared else generation,
            )
        )
    net_q = sum(
        sum((b.q_mvar.low + b.q_mvar.high) / 2 for b in f.loads) for f in envelopes
    ) / len(envelopes) - sum(network.shunt_q_mvar)
    terminal = TubeCommand(
        p_mw=tuple(0.0 for _ in devices.resources),
        q_mvar=tuple(
            min(r.q.high, max(r.q.low, net_q)) if r.kind == "svg" else 0.0
            for r in devices.resources
        ),
    )
    devices = devices.model_copy(
        update={
            "version": "unified-device-v1",
            "settling_resolution": research.settling_resolution,
        }
    )
    identity = sha256(
        (
            network.model_dump_json()
            + devices.model_dump_json()
            + research.model_dump_json()
            + (energy_task.model_dump_json() if energy_task else "")
            + "".join(f.model_dump_json() for f in envelopes)
        ).encode()
    ).hexdigest()
    devices = devices.model_copy(update={"context_id": identity})
    return UnifiedDomain(
        research=research,
        bound_source=declared.source if declared else research.source,
        network=network,
        devices=devices,
        forecasts=tuple(envelopes),
        terminal_command=terminal,
        p_band_mw=p_band_mw,
        q_band_mvar=q_band_mvar,
        response_steps=response_steps,
        context_id=identity,
        energy_task=energy_task,
        economic_prices_cny_per_mwh=economic_prices_cny_per_mwh,
        economic_rates=economic_rates,
    )

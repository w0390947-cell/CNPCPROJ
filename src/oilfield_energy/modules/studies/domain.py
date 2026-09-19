"""Seeded, bounded analytic forecast and plant trajectories; no I/O."""

import math
import random
from datetime import timedelta

from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs

from .contracts import StudySpec


def generate_profiles(spec: StudySpec, *, resolution: int, forecast: str) -> PlantInputs:
    if forecast not in ("day_ahead", "intraday", "plant") or resolution not in (1, 15):
        raise ValueError("unsupported profile mode")
    rng = random.Random(spec.seed + {"day_ahead": 0, "intraday": 1, "plant": 2}[forecast])
    count = spec.intervals * spec.interval_minutes // resolution
    times = [spec.start + timedelta(minutes=i * resolution) for i in range(count)]
    hours = [t.hour + t.minute / 60 for t in times]
    error = {"day_ahead": 0.025, "intraday": 0.012, "plant": 0.004}[forecast]
    load_p: list[BusSeries] = []
    load_q: list[BusSeries] = []
    wind: list[BusSeries] = []
    pv: list[BusSeries] = []
    for index, load in enumerate(spec.loads):
        values = tuple(
            round(
                load.peak_mw
                * (
                    0.72
                    + 0.09 * math.exp(-(((h - 9) / 2.5) ** 2))
                    + 0.18 * math.exp(-(((h - 19) / 3) ** 2))
                    + 0.02 * math.sin(2 * math.pi * (h + index) / 24)
                )
                * (1 + rng.uniform(-error, error)),
                7,
            )
            for h in hours
        )
        load_p.append(BusSeries(bus_id=load.bus_id, values=values))
        load_q.append(
            BusSeries(
                bus_id=load.bus_id,
                values=tuple(round(v * math.tan(math.acos(load.power_factor)), 7) for v in values),
            )
        )
    for index, resource in enumerate(spec.resources):
        if resource.kind == "wind":
            values = tuple(
                round(
                    resource.p_max_mw
                    * min(
                        0.90,
                        max(
                            0.15,
                            0.46
                            + 0.14 * math.sin(2 * math.pi * (h + index * 2) / 24)
                            + 0.06 * math.sin(2 * math.pi * h / 7)
                            + rng.uniform(-error, error),
                        ),
                    ),
                    7,
                )
                for h in hours
            )
            wind.append(BusSeries(bus_id=resource.bus_id, values=values))
        elif resource.kind == "pv":
            values = tuple(
                round(
                    resource.p_max_mw
                    * min(
                        1.0,
                        max(
                            0.0,
                            max(0.0, math.sin(math.pi * (h - 6) / 12)) ** 1.6
                            * (1 - 0.15 * math.exp(-(((h - 13.3) / 0.6) ** 2)))
                            * (1 + rng.uniform(-error, error)),
                        ),
                    ),
                    7,
                )
                for h in hours
            )
            pv.append(BusSeries(bus_id=resource.bus_id, values=values))
    for bus in spec.bus_ids:
        if bus not in {load.bus_id for load in spec.loads}:
            load_p.append(BusSeries(bus_id=bus, values=(0.0,) * count))
            load_q.append(BusSeries(bus_id=bus, values=(0.0,) * count))
    return PlantInputs(
        start=spec.start,
        step_minutes=resolution,
        load_p=tuple(load_p),
        load_q=tuple(load_q),
        wind_available=tuple(wind),
        pv_available=tuple(pv),
    )

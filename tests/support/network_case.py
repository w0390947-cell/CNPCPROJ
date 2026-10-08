"""Synthetic three-region radial feeder; no customer data."""

import numpy as np

from oilfield_energy.data import (
    Line,
    MicrogridData,
    ModelAssumptions,
    ProjectCase,
    Storage,
)
from oilfield_energy.network_model import build_legacy_network_model_v2


def bottleneck_case(*, rating=1.0, voltage_max=1.05, steps=1):
    regions = []
    for name in ("A", "B", "C"):
        buses = ["pcc", "load", "pv"]
        lines = [
            Line("load_line", "pcc", "load", 0.01, 0.01, 10.0),
            Line("pv_line", "pcc", "pv", 0.01, 0.01, rating),
        ]
        network = build_legacy_network_model_v2(
            network_id=name,
            buses=buses,
            pcc_bus_id="pcc",
            base_mva=10.0,
            lines=lines,
        )
        regions.append(
            MicrogridData(
                name=name,
                buses=buses,
                lines=lines,
                pcc_bus="pcc",
                base_mva=10.0,
                maximum_load_mw=5.0,
                p_grid_max_mw=10.0,
                p_grid_min_mw=0.15,
                voltage_min_pu=0.95,
                voltage_max_pu=voltage_max,
                load_p_mw={
                    b: np.full(steps, 5.0 if b == "load" else 0.0) for b in buses
                },
                load_q_mvar={b: np.zeros(steps) for b in buses},
                wind_available_mw={},
                wind_capacity_mw={},
                wind_capacity_mva={},
                pv_available_mw={"pv": np.full(steps, 4.0)},
                pv_capacity_mw={"pv": 4.0},
                pv_capacity_mva={"pv": 4.4},
                storage=Storage("load", 1.0, 4.0, 0.5, 2.0, 0.95, 0.95, 1.2),
                svg_bus="load",
                svg_q_min_mvar=0.0,
                svg_q_max_mvar=0.0,
                pv_reactive_enabled={"pv": False},
                storage_reactive_enabled=False,
                network_model_v2=network,
                network_operating_mode_id=network.default_operating_mode_id,
            )
        )
    return ProjectCase(
        np.arange(steps) / 4, np.full(steps, 600.0), regions, ModelAssumptions(), 20.0
    )

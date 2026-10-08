"""Small stationary three-region reserve fixture; entirely synthetic."""

from dataclasses import replace

import numpy as np

from tests.support.network_case import bottleneck_case


def stationary_reserve_case():
    case = bottleneck_case(rating=10, steps=4)
    grids = []
    for name, mg in zip(("SC", "YA_B", "YA_C"), case.microgrids, strict=True):
        grids.append(
            replace(
                mg,
                name=name,
                maximum_load_mw=2,
                load_p_mw={
                    b: np.full(4, 2.0 if b == "load" else 0.0) for b in mg.buses
                },
                pv_available_mw={"pv": np.full(4, 0.1)},
                pv_reactive_enabled={"pv": True},
                wind_available_mw={"load": np.full(4, 0.1)},
                wind_capacity_mw={"load": 1.0},
                wind_capacity_mva={"load": 1.2},
                storage_reactive_enabled=True,
                svg_q_min_mvar=-0.5,
                svg_q_max_mvar=0.5,
                storage=replace(mg.storage, p_max_mw=2, s_max_mva=2.2),
            )
        )
    return replace(case, microgrids=grids)

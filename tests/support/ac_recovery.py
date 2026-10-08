"""Synthetic 3-bus AC consistency counterexample; no customer measurements."""

from dataclasses import replace

import numpy as np

from tests.support.network_case import bottleneck_case


def ac_tracking_case(*, steps=1, regions=1):
    case = bottleneck_case(rating=10, steps=steps)
    grids = [
        replace(
            mg,
            maximum_load_mw=7.0,
            load_p_mw={
                b: np.full(steps, 7.0 if b == "load" else 0.0) for b in mg.buses
            },
            pv_available_mw={"pv": np.zeros(steps)},
        )
        for mg in case.microgrids[:regions]
    ]
    return replace(case, microgrids=grids, cluster_import_limit_mw=30.0)


def targets(case):
    return {
        m.name: {
            "p_mw": np.full(len(case.time_hours), 7.05),
            "q_mvar": np.full(len(case.time_hours), 0.05),
        }
        for m in case.microgrids
    }

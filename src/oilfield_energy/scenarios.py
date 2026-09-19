"""日前预测到日内更新的确定性扰动场景。"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .data import ProjectCase


def _smooth_factor(rng: np.random.Generator, length: int, standard_deviation: float) -> np.ndarray:
    raw = rng.normal(0.0, standard_deviation, length + 4)
    kernel = np.asarray([0.10, 0.20, 0.40, 0.20, 0.10])
    smooth = np.convolve(raw, kernel, mode="valid")[:length]
    return np.clip(1.0 + smooth, 0.80, 1.20)


def build_intraday_updated_case(
    case: ProjectCase,
    *,
    seed: int = 20260831,
    load_standard_deviation: float = 0.025,
    renewable_standard_deviation: float = 0.055,
) -> ProjectCase:
    """用平滑预测误差构造15分钟级日内更新场景。"""
    rng = np.random.default_rng(seed)
    T = len(case.time_hours)
    updated = []
    for mg in case.microgrids:
        load_factor = _smooth_factor(rng, T, load_standard_deviation)
        wind_factor = _smooth_factor(rng, T, renewable_standard_deviation)
        pv_factor = _smooth_factor(rng, T, renewable_standard_deviation)
        load_p = {bus: np.asarray(values) * load_factor for bus, values in mg.load_p_mw.items()}
        load_q = {bus: np.asarray(values) * load_factor for bus, values in mg.load_q_mvar.items()}
        wind = {
            bus: np.maximum(0.0, np.asarray(values) * wind_factor)
            for bus, values in mg.wind_available_mw.items()
        }
        pv = {
            bus: np.maximum(0.0, np.asarray(values) * pv_factor)
            for bus, values in mg.pv_available_mw.items()
        }
        updated.append(replace(
            mg,
            load_p_mw=load_p,
            load_q_mvar=load_q,
            wind_available_mw=wind,
            pv_available_mw=pv,
        ))
    return replace(case, microgrids=updated)


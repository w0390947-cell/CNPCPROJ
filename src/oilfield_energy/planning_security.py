"""计划层PCC时变安全下界与倒送风险场景估计。

本模块只使用当前计划版本中的负荷、风电和光伏预测。随机场景由固定种子
生成，不读取设备仿真产生的未来实测轨迹，因而不会产生前视信息泄漏。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import ceil
from zlib import crc32

import numpy as np

from .data import MicrogridData, ProjectCase
from .group_control import calculate_thresholds
from .hierarchy_types import (
    GroupControlConfig,
    GroupControlInput,
    PlanningSecurityTrajectory,
)


def _sum_profiles(profiles: Mapping[str, np.ndarray], steps: int) -> np.ndarray:
    if not profiles:
        return np.zeros(steps, dtype=float)
    values = np.vstack([np.asarray(profile, dtype=float) for profile in profiles.values()])
    if values.shape[1] != steps:
        raise ValueError(f"profile length must equal {steps}")
    return np.sum(values, axis=0)


def estimate_reverse_flow_probability(
    load_forecast_mw: np.ndarray,
    wind_forecast_mw: np.ndarray,
    pv_forecast_mw: np.ndarray,
    *,
    dt_minutes: float,
    config: GroupControlConfig,
    random_seed: int,
) -> np.ndarray:
    """用负荷/光伏预测误差场景估计窗口内至少一次倒送的概率。

    风电暂按计划预测确定值处理；该边界与项目参考文档保持一致，后续取得
    风电预测误差统计后可扩充场景接口，而无需改变计划模型约束。
    """
    load = np.asarray(load_forecast_mw, dtype=float)
    wind = np.asarray(wind_forecast_mw, dtype=float)
    pv = np.asarray(pv_forecast_mw, dtype=float)
    if load.ndim != 1 or wind.shape != load.shape or pv.shape != load.shape:
        raise ValueError("load, wind and PV forecasts must be one-dimensional and aligned")
    if not np.all(np.isfinite(load)) or not np.all(np.isfinite(wind)) or not np.all(np.isfinite(pv)):
        raise ValueError("planning forecasts must be finite")
    if np.min(load) < 0.0 or np.min(wind) < 0.0 or np.min(pv) < 0.0:
        raise ValueError("planning forecasts must be nonnegative")
    if not np.isfinite(dt_minutes) or dt_minutes <= 0.0:
        raise ValueError("dt_minutes must be positive and finite")

    rng = np.random.default_rng(random_seed)
    scenarios = config.planning_scenario_count
    load_error = rng.normal(
        0.0,
        config.planning_load_forecast_std_ratio,
        size=(scenarios, load.size),
    )
    pv_error = rng.normal(
        0.0,
        config.planning_pv_forecast_std_ratio,
        size=(scenarios, pv.size),
    )
    scenario_load = np.maximum(0.0, load[None, :] * (1.0 + load_error))
    scenario_pv = np.maximum(0.0, pv[None, :] * (1.0 + pv_error))
    # 与ADMM无网络聚合模型相同，规划期先加入确定性的有功损耗容许量。
    scenario_pcc = scenario_load + 0.008 * load[None, :] - wind[None, :] - scenario_pv

    horizon_steps = max(1, int(ceil(config.risk_probability_horizon_minutes / dt_minutes)))
    probability = np.zeros(load.size, dtype=float)
    for t in range(load.size):
        stop = min(load.size, t + horizon_steps)
        probability[t] = float(np.mean(np.any(scenario_pcc[:, t:stop] < 0.0, axis=1)))
    return probability


def build_planning_security_trajectory(
    case: ProjectCase,
    microgrid: MicrogridData,
    *,
    config: GroupControlConfig | None = None,
) -> PlanningSecurityTrajectory:
    """根据一个区域的计划预测构造逐时风险特征与唯一有效安全下界。"""
    cfg = config or GroupControlConfig()
    steps = len(case.time_hours)
    load = _sum_profiles(microgrid.load_p_mw, steps)
    wind = _sum_profiles(microgrid.wind_available_mw, steps)
    pv = _sum_profiles(microgrid.pv_available_mw, steps)
    net_load = load + 0.008 * load - wind - pv
    stable_name_seed = crc32(microgrid.name.encode("utf-8")) & 0xFFFFFFFF
    probability = estimate_reverse_flow_probability(
        load,
        wind,
        pv,
        dt_minutes=case.assumptions.dt_hours * 60.0,
        config=cfg,
        random_seed=(cfg.planning_random_seed + stable_name_seed) % (2**32),
    )

    load_change_rate = np.zeros(steps, dtype=float)
    risk_index = np.zeros(steps, dtype=float)
    calculated = np.zeros(steps, dtype=float)
    pv_capacity = float(sum(microgrid.pv_capacity_mw.values()))
    for t in range(steps):
        previous = net_load[t - 1] if t > 0 else net_load[t]
        snapshot = calculate_thresholds(
            GroupControlInput(
                time_minutes=float(t * case.assumptions.dt_hours * 60.0),
                pcc_power_mw=float(net_load[t]),
                current_net_load_mw=float(net_load[t]),
                previous_net_load_mw=float(previous),
                maximum_load_mw=microgrid.maximum_load_mw,
                pv_capacity_mw=pv_capacity,
                reverse_flow_probability=float(probability[t]),
                p_grid_max_mw=microgrid.p_grid_max_mw,
            ),
            config=cfg,
        )
        load_change_rate[t] = snapshot.load_change_rate
        risk_index[t] = snapshot.risk_index
        calculated[t] = snapshot.safety_threshold_mw

    fixed_floor = max(microgrid.p_grid_min_mw, case.assumptions.no_reverse_margin_mw)
    effective = np.maximum(calculated, fixed_floor)
    if np.max(effective) > microgrid.p_grid_max_mw + 1e-12:
        raise ValueError(f"{microgrid.name} planning security floor exceeds PCC capacity")
    return PlanningSecurityTrajectory(
        name=microgrid.name,
        net_load_forecast_mw=net_load,
        load_change_rate=load_change_rate,
        reverse_flow_probability=probability,
        risk_index=risk_index,
        calculated_safety_threshold_mw=calculated,
        effective_floor_mw=effective,
    )


def build_planning_security_trajectories(
    case: ProjectCase,
    microgrid_names: Sequence[str] | None = None,
    *,
    config: GroupControlConfig | None = None,
) -> dict[str, PlanningSecurityTrajectory]:
    """为所选区域生成可重复的计划安全轨迹，并检查集群可行性。"""
    lookup = {microgrid.name: microgrid for microgrid in case.microgrids}
    names = list(microgrid_names) if microgrid_names is not None else list(lookup)
    missing = [name for name in names if name not in lookup]
    if missing:
        raise KeyError(f"unknown microgrids: {missing}")
    trajectories = {
        name: build_planning_security_trajectory(case, lookup[name], config=config)
        for name in names
    }
    if trajectories:
        aggregate_floor = np.sum(
            [item.effective_floor_mw for item in trajectories.values()],
            axis=0,
        )
        if np.max(aggregate_floor) > case.cluster_import_limit_mw + 1e-12:
            raise ValueError("aggregate planning security floors exceed cluster import limit")
    return trajectories


def resolve_security_floors(
    case: ProjectCase,
    microgrids: Sequence[MicrogridData],
    security_floors_mw: Mapping[str, np.ndarray] | None,
    *,
    config: GroupControlConfig | None = None,
) -> dict[str, np.ndarray]:
    """验证外部轨迹，或生成默认动态轨迹，返回防御性副本。"""
    if security_floors_mw is None:
        generated = build_planning_security_trajectories(
            case,
            [microgrid.name for microgrid in microgrids],
            config=config,
        )
        return {name: item.effective_floor_mw.copy() for name, item in generated.items()}

    expected_names = {microgrid.name for microgrid in microgrids}
    missing = sorted(expected_names.difference(security_floors_mw))
    if missing:
        raise KeyError(f"missing planning security floors for: {missing}")
    steps = len(case.time_hours)
    floors: dict[str, np.ndarray] = {}
    for microgrid in microgrids:
        floor = np.asarray(security_floors_mw[microgrid.name], dtype=float)
        if floor.shape != (steps,):
            raise ValueError(f"{microgrid.name} security floor must contain {steps} time steps")
        if not np.all(np.isfinite(floor)):
            raise ValueError(f"{microgrid.name} security floor must be finite")
        fixed_floor = max(microgrid.p_grid_min_mw, case.assumptions.no_reverse_margin_mw)
        if np.min(floor) < fixed_floor - 1e-12:
            raise ValueError(f"{microgrid.name} security floor is below the fixed safety minimum")
        if np.max(floor) > microgrid.p_grid_max_mw + 1e-12:
            raise ValueError(f"{microgrid.name} security floor exceeds PCC capacity")
        floors[microgrid.name] = floor.copy()
    if floors and np.max(np.sum(list(floors.values()), axis=0)) > case.cluster_import_limit_mw + 1e-12:
        raise ValueError("aggregate planning security floors exceed cluster import limit")
    return floors


__all__ = [
    "build_planning_security_trajectories",
    "build_planning_security_trajectory",
    "estimate_reverse_flow_probability",
    "resolve_security_floors",
]

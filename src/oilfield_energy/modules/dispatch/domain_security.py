"""Deterministic planning safety thresholds; explicit inputs, no I/O or actuation."""

from math import isfinite
from numbers import Real

from .contracts import (
    PlanningSecurityConfig,
    SecurityThresholdInput,
    SecurityThresholds,
)


def _require_finite(name: str, value: float) -> float:
    """返回有限浮点值，否则给出包含参数名的异常。"""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    converted = float(value)
    if not isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _clip_unit_interval(value: float) -> tuple[float, bool]:
    """将可超过参考量的归一化特征限制到``[0, 1]``。"""
    clipped = min(1.0, max(0.0, value))
    return clipped, clipped != value


def calculate_load_change_rate(
    current_net_load_mw: float,
    previous_net_load_mw: float,
    *,
    denominator_floor_mw: float,
) -> float:
    """计算相邻15分钟净负荷的相对变化率。

    ``denominator_floor_mw``避免上一时刻净负荷接近零时产生无穷大或
    数值上没有工程意义的变化率。函数保留变化率符号；紧急判定阶段再
    根据参考策略使用其绝对值。
    """
    current = _require_finite("current_net_load_mw", current_net_load_mw)
    previous = _require_finite("previous_net_load_mw", previous_net_load_mw)
    floor = _require_finite("denominator_floor_mw", denominator_floor_mw)
    if floor <= 0.0:
        raise ValueError("denominator_floor_mw must be positive")
    return (current - previous) / max(abs(previous), floor)


def calculate_pv_penetration(pv_capacity_mw: float, maximum_load_mw: float) -> float:
    """计算控制区域光伏装机容量与最大负荷之比。"""
    capacity = _require_finite("pv_capacity_mw", pv_capacity_mw)
    maximum_load = _require_finite("maximum_load_mw", maximum_load_mw)
    if capacity < 0.0:
        raise ValueError("pv_capacity_mw must be nonnegative")
    if maximum_load <= 0.0:
        raise ValueError("maximum_load_mw must be positive")
    return capacity / maximum_load


def _normalized_risk_features(
    pv_penetration: float,
    load_change_rate: float,
    reverse_flow_probability: float,
    config: PlanningSecurityConfig,
) -> tuple[float, float, float, bool]:
    penetration = _require_finite("pv_penetration", pv_penetration)
    change_rate = _require_finite("load_change_rate", load_change_rate)
    probability = _require_finite("reverse_flow_probability", reverse_flow_probability)
    if penetration < 0.0:
        raise ValueError("pv_penetration must be nonnegative")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("reverse_flow_probability must be in [0, 1]")

    normalized_penetration, penetration_clipped = _clip_unit_interval(
        penetration / config.pv_penetration_reference
    )
    normalized_change_rate, change_rate_clipped = _clip_unit_interval(
        abs(change_rate) / config.load_change_rate_reference
    )
    risk_index = (
        config.weight_pv_penetration * normalized_penetration
        + config.weight_load_change_rate * normalized_change_rate
        + config.weight_reverse_flow_probability * probability
    )
    # 浮点舍入不应使理论上位于单位区间内的加权和越界。
    risk_index = min(1.0, max(0.0, risk_index))
    return (
        normalized_penetration,
        normalized_change_rate,
        risk_index,
        penetration_clipped or change_rate_clipped,
    )


def calculate_risk_index(
    pv_penetration: float,
    load_change_rate: float,
    reverse_flow_probability: float,
    *,
    config: PlanningSecurityConfig | None = None,
) -> float:
    """根据光伏渗透率、负荷变化率和倒送概率计算综合风险指数。"""
    cfg = config or PlanningSecurityConfig()
    return _normalized_risk_features(
        pv_penetration,
        load_change_rate,
        reverse_flow_probability,
        cfg,
    )[2]


def calculate_thresholds(
    control_input: SecurityThresholdInput,
    *,
    config: PlanningSecurityConfig | None = None,
) -> SecurityThresholds:
    """计算一个判断时刻的安全阈值、风险限值和恢复阈值。

    阈值功率均按``maximum_load_mw``缩放。若区域PCC最大受电能力不足以
    容纳计算得到的恢复阈值，函数拒绝该配置，而不是静默破坏三重阈值
    的严格顺序。
    """
    cfg = config or PlanningSecurityConfig()
    time_minutes = _require_finite("time_minutes", control_input.time_minutes)
    _require_finite("pcc_power_mw", control_input.pcc_power_mw)
    current_load = _require_finite(
        "current_net_load_mw", control_input.current_net_load_mw
    )
    previous_load = _require_finite(
        "previous_net_load_mw", control_input.previous_net_load_mw
    )
    maximum_load = _require_finite("maximum_load_mw", control_input.maximum_load_mw)
    pv_capacity = _require_finite("pv_capacity_mw", control_input.pv_capacity_mw)
    probability = _require_finite(
        "reverse_flow_probability", control_input.reverse_flow_probability
    )
    p_grid_max = _require_finite("p_grid_max_mw", control_input.p_grid_max_mw)

    if time_minutes < 0.0:
        raise ValueError("time_minutes must be nonnegative")
    if maximum_load <= 0.0:
        raise ValueError("maximum_load_mw must be positive")
    if pv_capacity < 0.0:
        raise ValueError("pv_capacity_mw must be nonnegative")
    if p_grid_max <= 0.0:
        raise ValueError("p_grid_max_mw must be positive")

    denominator_floor = maximum_load * cfg.load_change_denominator_floor_ratio
    load_change_rate = calculate_load_change_rate(
        current_load,
        previous_load,
        denominator_floor_mw=denominator_floor,
    )
    pv_penetration = calculate_pv_penetration(pv_capacity, maximum_load)
    (
        normalized_penetration,
        normalized_change_rate,
        risk_index,
        was_clipped,
    ) = _normalized_risk_features(
        pv_penetration,
        load_change_rate,
        probability,
        cfg,
    )

    safety_threshold = maximum_load * (
        cfg.base_safety_margin_ratio + cfg.maximum_safety_increment_ratio * risk_index
    )
    risk_buffer = maximum_load * (
        cfg.maximum_risk_buffer_ratio
        - (cfg.maximum_risk_buffer_ratio - cfg.minimum_risk_buffer_ratio) * risk_index
    )
    restore_buffer = maximum_load * (
        cfg.minimum_restore_buffer_ratio
        + (cfg.maximum_restore_buffer_ratio - cfg.minimum_restore_buffer_ratio)
        * risk_index
    )
    risk_limit = safety_threshold - risk_buffer
    restore_threshold = safety_threshold + restore_buffer

    if not 0.0 < risk_limit < safety_threshold < restore_threshold:
        raise ValueError(
            "planning-security configuration does not produce strictly ordered positive thresholds"
        )
    if restore_threshold > p_grid_max:
        raise ValueError(
            "restore threshold exceeds p_grid_max_mw; adjust planning-security ratios or area capacity"
        )

    return SecurityThresholds(
        load_change_rate=load_change_rate,
        pv_penetration=pv_penetration,
        normalized_load_change_rate=normalized_change_rate,
        normalized_pv_penetration=normalized_penetration,
        reverse_flow_probability=probability,
        risk_index=risk_index,
        safety_threshold_mw=safety_threshold,
        risk_limit_mw=risk_limit,
        restore_threshold_mw=restore_threshold,
        risk_buffer_mw=risk_buffer,
        restore_buffer_mw=restore_buffer,
        was_clipped=was_clipped,
    )

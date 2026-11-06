"""Immutable planning-security parameters and values, with no control state machine."""

from dataclasses import dataclass
from math import isclose, isfinite
from numbers import Integral, Real


@dataclass(frozen=True)
class PlanningSecurityConfig:
    """Planning risk and threshold parameters; synthetic defaults, not field settings."""

    load_change_denominator_floor_ratio: float = 0.01
    pv_penetration_reference: float = 1.0
    load_change_rate_reference: float = 0.1
    weight_pv_penetration: float = 0.3
    weight_load_change_rate: float = 0.4
    weight_reverse_flow_probability: float = 0.3
    base_safety_margin_ratio: float = 0.015
    maximum_safety_increment_ratio: float = 0.03
    minimum_risk_buffer_ratio: float = 0.003
    maximum_risk_buffer_ratio: float = 0.01
    minimum_restore_buffer_ratio: float = 0.01
    maximum_restore_buffer_ratio: float = 0.03
    risk_probability_horizon_minutes: int = 15
    planning_scenario_count: int = 200
    planning_load_forecast_std_ratio: float = 0.03
    planning_pv_forecast_std_ratio: float = 0.08
    planning_random_seed: int = 20260906

    def __post_init__(self) -> None:
        """Reject invalid planning risk and threshold parameters."""
        real_values = {
            "load_change_denominator_floor_ratio": self.load_change_denominator_floor_ratio,
            "pv_penetration_reference": self.pv_penetration_reference,
            "load_change_rate_reference": self.load_change_rate_reference,
            "weight_pv_penetration": self.weight_pv_penetration,
            "weight_load_change_rate": self.weight_load_change_rate,
            "weight_reverse_flow_probability": self.weight_reverse_flow_probability,
            "base_safety_margin_ratio": self.base_safety_margin_ratio,
            "maximum_safety_increment_ratio": self.maximum_safety_increment_ratio,
            "minimum_risk_buffer_ratio": self.minimum_risk_buffer_ratio,
            "maximum_risk_buffer_ratio": self.maximum_risk_buffer_ratio,
            "minimum_restore_buffer_ratio": self.minimum_restore_buffer_ratio,
            "maximum_restore_buffer_ratio": self.maximum_restore_buffer_ratio,
            "planning_load_forecast_std_ratio": self.planning_load_forecast_std_ratio,
            "planning_pv_forecast_std_ratio": self.planning_pv_forecast_std_ratio,
        }
        non_real = [
            name
            for name, value in real_values.items()
            if isinstance(value, bool) or not isinstance(value, Real)
        ]
        if non_real:
            raise ValueError(
                f"planning-security parameters must be real numbers: {', '.join(non_real)}"
            )
        non_finite = [
            name for name, value in real_values.items() if not isfinite(float(value))
        ]
        if non_finite:
            raise ValueError(
                f"planning-security parameters must be finite: {', '.join(non_finite)}"
            )
        positive_values = {
            "load_change_denominator_floor_ratio": self.load_change_denominator_floor_ratio,
            "pv_penetration_reference": self.pv_penetration_reference,
            "load_change_rate_reference": self.load_change_rate_reference,
            "base_safety_margin_ratio": self.base_safety_margin_ratio,
            "minimum_risk_buffer_ratio": self.minimum_risk_buffer_ratio,
            "maximum_risk_buffer_ratio": self.maximum_risk_buffer_ratio,
            "minimum_restore_buffer_ratio": self.minimum_restore_buffer_ratio,
            "maximum_restore_buffer_ratio": self.maximum_restore_buffer_ratio,
        }
        non_positive = [name for name, value in positive_values.items() if value <= 0.0]
        if non_positive:
            raise ValueError(
                f"planning-security parameters must be positive: {', '.join(non_positive)}"
            )
        if self.maximum_safety_increment_ratio < 0.0:
            raise ValueError("maximum_safety_increment_ratio must be nonnegative")
        if self.planning_load_forecast_std_ratio < 0.0:
            raise ValueError("planning_load_forecast_std_ratio must be nonnegative")
        if self.planning_pv_forecast_std_ratio < 0.0:
            raise ValueError("planning_pv_forecast_std_ratio must be nonnegative")
        if self.minimum_risk_buffer_ratio > self.maximum_risk_buffer_ratio:
            raise ValueError(
                "minimum_risk_buffer_ratio cannot exceed maximum_risk_buffer_ratio"
            )
        if self.minimum_restore_buffer_ratio > self.maximum_restore_buffer_ratio:
            raise ValueError(
                "minimum_restore_buffer_ratio cannot exceed maximum_restore_buffer_ratio"
            )
        if self.base_safety_margin_ratio <= self.maximum_risk_buffer_ratio:
            raise ValueError(
                "base_safety_margin_ratio must exceed maximum_risk_buffer_ratio so that the risk limit remains positive"
            )
        weights = (
            self.weight_pv_penetration,
            self.weight_load_change_rate,
            self.weight_reverse_flow_probability,
        )
        if any((weight < 0.0 for weight in weights)):
            raise ValueError("planning-security risk weights must be nonnegative")
        if not isclose(sum(weights), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("planning-security risk weights must sum to 1")
        integer_values = {
            "risk_probability_horizon_minutes": self.risk_probability_horizon_minutes,
            "planning_scenario_count": self.planning_scenario_count,
        }
        non_integers = [
            name
            for name, value in integer_values.items()
            if isinstance(value, bool) or not isinstance(value, Integral)
        ]
        if non_integers:
            raise ValueError(
                f"planning-security interval and count parameters must be integers: {', '.join(non_integers)}"
            )
        non_positive_integers = [
            name for name, value in integer_values.items() if value <= 0
        ]
        if non_positive_integers:
            raise ValueError(
                f"planning-security interval and count parameters must be positive: {', '.join(non_positive_integers)}"
            )
        if (
            isinstance(self.planning_random_seed, bool)
            or not isinstance(self.planning_random_seed, Integral)
            or self.planning_random_seed < 0
        ):
            raise ValueError("planning_random_seed must be a nonnegative integer")


@dataclass(frozen=True)
class SecurityThresholdInput:
    """One forecast sample: time in minutes, power in MW, risk probability in [0, 1]."""

    time_minutes: float
    pcc_power_mw: float
    current_net_load_mw: float
    previous_net_load_mw: float
    maximum_load_mw: float
    pv_capacity_mw: float
    reverse_flow_probability: float
    p_grid_max_mw: float


@dataclass(frozen=True)
class SecurityThresholds:
    """Pure planning risk features and threshold values in MW; no runtime state."""

    load_change_rate: float
    pv_penetration: float
    normalized_load_change_rate: float
    normalized_pv_penetration: float
    reverse_flow_probability: float
    risk_index: float
    safety_threshold_mw: float
    risk_limit_mw: float
    restore_threshold_mw: float
    risk_buffer_mw: float
    restore_buffer_mw: float
    was_clipped: bool = False

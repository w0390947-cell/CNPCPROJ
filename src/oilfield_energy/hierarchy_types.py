"""多层级协调、通信和设备跟踪的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from numbers import Integral
from typing import Dict, List

import numpy as np

from .modules.control.contracts import RestorationEvidence, StorageDynamicsRecord
from .modules.dispatch.contracts import (
    ComputationQualityPolicy,
    CoordinationBudgetEvidence,
    CoordinationIterationTrace,
    CoordinationSnapshot,
    DispatchCapabilities,
    EconomicCost,
    RenewableAccounting,
)
from .modules.studies.contracts import (
    CommunicationEventExecution,
    CommunicationFaultWindow,
)


@dataclass(frozen=True)
class ADMMConfig:
    rho: float = 120.0
    max_iterations: int = 200
    min_iterations: int = 4
    absolute_tolerance: float = 2e-3
    relative_tolerance: float = 2e-3
    consecutive_convergence_iterations: int = 3
    aggregate_pf_target: float = 0.95
    local_q_regularization: float = 2.0
    local_ramp_regularization: float = 12.0
    solver: str = "CLARABEL"
    quality_policy: ComputationQualityPolicy | None = None
    # Window-specific sufficient margin, Mvar, applied to both signed Q faces.
    aggregate_pf_margin_mvar: tuple[float, ...] = ()
    network_model: str = "aggregate-v1"

    def __post_init__(self) -> None:
        if self.network_model not in {"aggregate-v1", "branch-flow-socp-v1"}:
            raise ValueError("unsupported coordination network model")
        if (
            isinstance(self.aggregate_pf_target, bool)
            or not isfinite(self.aggregate_pf_target)
            or not 0 < self.aggregate_pf_target <= 1
        ):
            raise ValueError("aggregate PF target must be in (0, 1]")
        if not isinstance(self.aggregate_pf_margin_mvar, tuple) or any(
            isinstance(v, bool) or not isfinite(v) or v < 0
            for v in self.aggregate_pf_margin_mvar
        ):
            raise ValueError(
                "aggregate PF margins must be immutable finite nonnegative Mvar"
            )


@dataclass(frozen=True)
class CommunicationConfig:
    loss_probability: float = 0.0
    loss_start_iteration: int | None = None
    loss_end_iteration: int | None = None
    min_delay_iterations: int = 0
    max_delay_iterations: int = 0
    stale_limit_iterations: int = 4
    outage_region: str | None = None
    outage_start_iteration: int | None = None
    outage_end_iteration: int | None = None
    random_seed: int = 20260830
    events: tuple[CommunicationFaultWindow, ...] = ()


@dataclass(frozen=True)
class TimeScaleConfig:
    day_ahead_step_minutes: int = 15
    intraday_update_minutes: int = 15
    intraday_horizon_hours: int = 4
    device_step_minutes: int = 1
    device_time_constant_minutes: float = 2.0
    pv_device_time_constant_minutes: float = 0.25
    active_power_ramp_mw_per_minute: float = 0.65
    reactive_power_ramp_mvar_per_minute: float = 0.90
    # 硬防倒送上限不能按“一周期动作、一周期释放”振荡。以下为开发仿真值，
    # 现场值必须结合考核点误差、扫描周期和保护定值重新整定。
    hard_release_hysteresis_mw: float = 0.05
    hard_release_confirmation_cycles: int = 2

    def __post_init__(self) -> None:
        if (
            isinstance(self.device_step_minutes, bool)
            or not isinstance(self.device_step_minutes, Integral)
            or self.device_step_minutes <= 0
        ):
            raise ValueError("device_step_minutes must be a positive integer")
        for value in (
            self.device_time_constant_minutes,
            self.pv_device_time_constant_minutes,
        ):
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError("device time constants must be finite and positive")
        for value in (
            self.active_power_ramp_mw_per_minute,
            self.reactive_power_ramp_mvar_per_minute,
        ):
            if isinstance(value, bool) or not isfinite(value) or value < 0:
                raise ValueError("device ramps must be finite and nonnegative")
        if (
            not isfinite(self.hard_release_hysteresis_mw)
            or self.hard_release_hysteresis_mw < 0.0
        ):
            raise ValueError("hard_release_hysteresis_mw must be nonnegative")
        if (
            isinstance(self.hard_release_confirmation_cycles, bool)
            or not isinstance(self.hard_release_confirmation_cycles, Integral)
            or self.hard_release_confirmation_cycles <= 0
        ):
            raise ValueError(
                "hard_release_confirmation_cycles must be a positive integer"
            )


@dataclass(frozen=True)
class PlanningSecurityTrajectory:
    """一个区域在计划时间尺度上的风险特征和有效PCC安全下界。"""

    name: str
    net_load_forecast_mw: np.ndarray
    load_change_rate: np.ndarray
    reverse_flow_probability: np.ndarray
    risk_index: np.ndarray
    calculated_safety_threshold_mw: np.ndarray
    effective_floor_mw: np.ndarray


@dataclass
class CoordinationSignal:
    p_reference_mw: np.ndarray
    q_reference_mvar: np.ndarray
    dual_p: np.ndarray
    dual_q: np.ndarray
    iteration: int


@dataclass
class RegionalSchedule:
    name: str
    p_grid_mw: np.ndarray
    q_grid_mvar: np.ndarray
    renewable_used_mw: np.ndarray
    storage_charge_mw: np.ndarray
    storage_discharge_mw: np.ndarray
    storage_energy_mwh: np.ndarray
    q_support_mvar: np.ndarray
    local_objective_cny: float
    status: str
    signal_iteration: int = -1
    used_fallback: bool = False
    economic_cost: EconomicCost | None = None
    renewable_accounting: RenewableAccounting | None = None


@dataclass
class ADMMIteration:
    iteration: int
    primal_residual: float
    dual_residual: float
    primal_tolerance: float
    dual_tolerance: float
    local_objective_cny: float
    fresh_region_count: int
    convergence_streak: int = 0
    fallback_regions: List[str] = field(default_factory=list)
    coordination: CoordinationIterationTrace | None = None


@dataclass
class CommunicationMetrics:
    sent: int = 0
    delivered: int = 0
    dropped: int = 0
    delayed: int = 0
    outage_dropped: int = 0
    stale_uses: int = 0
    fallback_uses: int = 0
    rejected_stale_messages: int = 0
    event_executions: tuple[CommunicationEventExecution, ...] = ()


@dataclass
class ADMMResult:
    converged: bool
    stop_reason: str
    iterations: int
    coordination_updates: int
    schedules: Dict[str, RegionalSchedule]
    p_references_mw: Dict[str, np.ndarray]
    q_references_mvar: Dict[str, np.ndarray]
    history: List[ADMMIteration]
    communication: CommunicationMetrics
    aggregate_import_mw: np.ndarray
    aggregate_q_mvar: np.ndarray
    # None means no complete barrier update was available, never certified.
    coordination_snapshot: CoordinationSnapshot | None = None
    capabilities: DispatchCapabilities = field(default_factory=DispatchCapabilities)
    quality_budgets: tuple[CoordinationBudgetEvidence, ...] = ()
    network_model: str = "aggregate-v1"


@dataclass
class PhysicalCoordinationReference:
    """PCC references from certified joint restoration; no ADMM convergence claim."""

    p_references_mw: Dict[str, np.ndarray]
    q_references_mvar: Dict[str, np.ndarray]
    history: tuple[ADMMIteration, ...] = ()
    quality_budgets: tuple[CoordinationBudgetEvidence, ...] = ()


ClusterReference = ADMMResult | PhysicalCoordinationReference


@dataclass
class DeviceTrackingResult:
    name: str
    time_minutes: np.ndarray
    pcc_command_mw: np.ndarray
    pcc_actual_mw: np.ndarray
    qcc_command_mvar: np.ndarray
    qcc_actual_mvar: np.ndarray
    p_tracking_rmse_mw: float
    q_tracking_rmse_mvar: float
    no_reverse_violations_before_safety: int
    no_reverse_violations_after_safety: int
    pf_violations_after_safety: int
    safety_interventions: int
    network_feedback: List[dict] = field(default_factory=list)
    network_context: dict = field(default_factory=dict)
    network_security_passed: bool = False
    network_invalid_steps: int = 0
    network_violation_steps: int = 0
    network_recovery_blocked: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    wind_command_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    wind_available_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    wind_actual_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    execution_evidence_version: str | None = None
    wind_resource_actual_mw: Dict[str, np.ndarray] = field(default_factory=dict)
    wind_availability_limited_mw: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    pv_availability_limited_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pv_plan_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pv_available_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pv_control_target_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pv_actual_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pv_station_plan_mw: Dict[str, np.ndarray] = field(default_factory=dict)
    pv_station_available_mw: Dict[str, np.ndarray] = field(default_factory=dict)
    pv_station_control_target_mw: Dict[str, np.ndarray] = field(default_factory=dict)
    pv_station_actual_mw: Dict[str, np.ndarray] = field(default_factory=dict)
    storage_actual_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    reactive_resource_plan_mvar: Dict[str, np.ndarray] = field(default_factory=dict)
    reactive_resource_target_mvar: Dict[str, np.ndarray] = field(default_factory=dict)
    reactive_resource_actual_mvar: Dict[str, np.ndarray] = field(default_factory=dict)
    wind_reactive_actual_mvar: np.ndarray = field(default_factory=lambda: np.empty(0))
    pv_reactive_actual_mvar: np.ndarray = field(default_factory=lambda: np.empty(0))
    storage_reactive_actual_mvar: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    svg_reactive_actual_mvar: np.ndarray = field(default_factory=lambda: np.empty(0))
    reactive_dispatch_unserved_mvar: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    shancheng_reactive_command_accepted: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    reactive_execution_known: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    hard_wind_storage_target_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    hard_safety_unserved_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    hard_wind_storage_command_accepted: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    measured_pv_curtailment_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pcc_before_local_safety_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    local_reverse_flow_reduction_mw: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    local_power_factor_adjustment_mvar: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    actual_power_factor: np.ndarray = field(default_factory=lambda: np.empty(0))
    storage_energy_mwh: np.ndarray = field(default_factory=lambda: np.empty(0))
    restoration_evidence: tuple[RestorationEvidence, ...] = ()
    storage_dynamics: tuple[StorageDynamicsRecord, ...] = ()
    storage_soc_within_limits: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    local_reverse_flow_interventions: int = 0
    local_power_factor_interventions: int = 0

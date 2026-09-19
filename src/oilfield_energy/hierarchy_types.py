"""多层级协调、通信和设备跟踪的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isclose, isfinite
from numbers import Integral, Real
from typing import Dict, List

import numpy as np

from .modules.control.contracts import RestorationEvidence, StorageDynamicsRecord
from .modules.dispatch.contracts import (
    CoordinationSnapshot,
    DispatchCapabilities,
    EconomicCost,
    RenewableAccounting,
)
from .modules.power_flow.contracts import ClusterValidation
from .modules.studies.contracts import CommunicationEventExecution, CommunicationFaultWindow


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
        for value in (self.device_time_constant_minutes, self.pv_device_time_constant_minutes):
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError("device time constants must be finite and positive")
        for value in (
            self.active_power_ramp_mw_per_minute,
            self.reactive_power_ramp_mvar_per_minute,
        ):
            if isinstance(value, bool) or not isfinite(value) or value < 0:
                raise ValueError("device ramps must be finite and nonnegative")
        if not isfinite(self.hard_release_hysteresis_mw) or self.hard_release_hysteresis_mw < 0.0:
            raise ValueError("hard_release_hysteresis_mw must be nonnegative")
        if (
            isinstance(self.hard_release_confirmation_cycles, bool)
            or not isinstance(self.hard_release_confirmation_cycles, Integral)
            or self.hard_release_confirmation_cycles <= 0
        ):
            raise ValueError("hard_release_confirmation_cycles must be a positive integer")


class GroupControlState(str, Enum):
    """分布式光伏群调群控监督器的离散运行状态。"""

    NORMAL = "normal"
    PREPARED = "prepared"
    RISK_OBSERVING = "risk_observing"
    CURTAILING = "curtailing"
    CURTAILED_HOLD = "curtailed_hold"
    RESTORE_WAIT = "restore_wait"
    RESTORING = "restoring"
    RECOVERY_ABORTED = "recovery_aborted"
    RECOVERY_INHIBIT = "recovery_inhibit"
    OUTPUT_BLOCK = "output_block"
    HARD_OVERRIDE = "hard_override"


class GroupControlAction(str, Enum):
    """群控监督器在一个判断周期内请求的动作。"""

    NONE = "none"
    CURTAIL = "curtail"
    HOLD = "hold"
    RESTORE = "restore"
    ABORT_RECOVERY = "abort_recovery"


class PVStationAvailability(str, Enum):
    """光伏场站参与本轮群控分配的可用状态。"""

    AVAILABLE = "available"
    OUT_OF_SERVICE = "out_of_service"
    FAULTED = "faulted"
    COMMUNICATION_LOST = "communication_lost"
    UNCONTROLLABLE = "uncontrollable"
    TELEMETRY_INVALID = "telemetry_invalid"
    EXECUTION_UNKNOWN = "execution_unknown"


@dataclass(frozen=True)
class GroupControlConfig:
    """群调群控算法配置。

    功率阈值使用区域最大负荷的比例定义，以便同一套算法适配不同容量
    等级的控制区域。默认值是模型骨架阶段的模拟参数，不代表现场整定值。
    """

    feature_interval_minutes: int = 15
    decision_interval_minutes: int = 1
    load_change_denominator_floor_ratio: float = 0.01
    pv_penetration_reference: float = 1.0
    load_change_rate_reference: float = 0.10
    weight_pv_penetration: float = 0.30
    weight_load_change_rate: float = 0.40
    weight_reverse_flow_probability: float = 0.30
    base_safety_margin_ratio: float = 0.015
    maximum_safety_increment_ratio: float = 0.030
    minimum_risk_buffer_ratio: float = 0.003
    maximum_risk_buffer_ratio: float = 0.010
    minimum_restore_buffer_ratio: float = 0.010
    maximum_restore_buffer_ratio: float = 0.030
    emergency_load_change_rate: float = 0.10
    consecutive_trigger_count: int = 3
    observation_window_size: int = 5
    observation_trigger_count: int = 3
    recovery_pause_minutes: int = 3
    recovery_step_fraction: float = 0.10
    response_relative_tolerance: float = 0.05
    risk_probability_horizon_minutes: int = 15
    planning_scenario_count: int = 200
    planning_load_forecast_std_ratio: float = 0.03
    planning_pv_forecast_std_ratio: float = 0.08
    planning_random_seed: int = 20260906

    def __post_init__(self) -> None:
        """尽早拒绝无法形成有效三重阈值或状态机的配置。"""
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
            "emergency_load_change_rate": self.emergency_load_change_rate,
            "recovery_step_fraction": self.recovery_step_fraction,
            "response_relative_tolerance": self.response_relative_tolerance,
            "planning_load_forecast_std_ratio": self.planning_load_forecast_std_ratio,
            "planning_pv_forecast_std_ratio": self.planning_pv_forecast_std_ratio,
        }
        non_real = [
            name for name, value in real_values.items()
            if isinstance(value, bool) or not isinstance(value, Real)
        ]
        if non_real:
            raise ValueError(
                f"group-control parameters must be real numbers: {', '.join(non_real)}"
            )
        non_finite = [
            name for name, value in real_values.items()
            if not isfinite(float(value))
        ]
        if non_finite:
            raise ValueError(f"group-control parameters must be finite: {', '.join(non_finite)}")

        positive_values = {
            "load_change_denominator_floor_ratio": self.load_change_denominator_floor_ratio,
            "pv_penetration_reference": self.pv_penetration_reference,
            "load_change_rate_reference": self.load_change_rate_reference,
            "base_safety_margin_ratio": self.base_safety_margin_ratio,
            "minimum_risk_buffer_ratio": self.minimum_risk_buffer_ratio,
            "maximum_risk_buffer_ratio": self.maximum_risk_buffer_ratio,
            "minimum_restore_buffer_ratio": self.minimum_restore_buffer_ratio,
            "maximum_restore_buffer_ratio": self.maximum_restore_buffer_ratio,
            "emergency_load_change_rate": self.emergency_load_change_rate,
        }
        non_positive = [name for name, value in positive_values.items() if value <= 0.0]
        if non_positive:
            raise ValueError(
                f"group-control parameters must be positive: {', '.join(non_positive)}"
            )
        if self.maximum_safety_increment_ratio < 0.0:
            raise ValueError("maximum_safety_increment_ratio must be nonnegative")
        if not 0.0 <= self.response_relative_tolerance <= 1.0:
            raise ValueError("response_relative_tolerance must be in [0, 1]")
        if not 0.0 < self.recovery_step_fraction <= 1.0:
            raise ValueError("recovery_step_fraction must be in (0, 1]")
        if self.planning_load_forecast_std_ratio < 0.0:
            raise ValueError("planning_load_forecast_std_ratio must be nonnegative")
        if self.planning_pv_forecast_std_ratio < 0.0:
            raise ValueError("planning_pv_forecast_std_ratio must be nonnegative")
        if self.minimum_risk_buffer_ratio > self.maximum_risk_buffer_ratio:
            raise ValueError("minimum_risk_buffer_ratio cannot exceed maximum_risk_buffer_ratio")
        if self.minimum_restore_buffer_ratio > self.maximum_restore_buffer_ratio:
            raise ValueError(
                "minimum_restore_buffer_ratio cannot exceed maximum_restore_buffer_ratio"
            )
        if self.base_safety_margin_ratio <= self.maximum_risk_buffer_ratio:
            raise ValueError(
                "base_safety_margin_ratio must exceed maximum_risk_buffer_ratio "
                "so that the risk limit remains positive"
            )

        weights = (
            self.weight_pv_penetration,
            self.weight_load_change_rate,
            self.weight_reverse_flow_probability,
        )
        if any(weight < 0.0 for weight in weights):
            raise ValueError("group-control risk weights must be nonnegative")
        if not isclose(sum(weights), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("group-control risk weights must sum to 1")

        integer_values = {
            "feature_interval_minutes": self.feature_interval_minutes,
            "decision_interval_minutes": self.decision_interval_minutes,
            "consecutive_trigger_count": self.consecutive_trigger_count,
            "observation_window_size": self.observation_window_size,
            "observation_trigger_count": self.observation_trigger_count,
            "recovery_pause_minutes": self.recovery_pause_minutes,
            "risk_probability_horizon_minutes": self.risk_probability_horizon_minutes,
            "planning_scenario_count": self.planning_scenario_count,
        }
        non_integers = [
            name for name, value in integer_values.items()
            if isinstance(value, bool) or not isinstance(value, Integral)
        ]
        if non_integers:
            raise ValueError(
                f"group-control interval and count parameters must be integers: "
                f"{', '.join(non_integers)}"
            )
        non_positive_integers = [name for name, value in integer_values.items() if value <= 0]
        if non_positive_integers:
            raise ValueError(
                f"group-control interval and count parameters must be positive: "
                f"{', '.join(non_positive_integers)}"
            )
        if self.feature_interval_minutes % self.decision_interval_minutes != 0:
            raise ValueError("decision_interval_minutes must divide feature_interval_minutes")
        if self.recovery_pause_minutes % self.decision_interval_minutes != 0:
            raise ValueError("decision_interval_minutes must divide recovery_pause_minutes")
        if self.risk_probability_horizon_minutes % self.decision_interval_minutes != 0:
            raise ValueError(
                "decision_interval_minutes must divide risk_probability_horizon_minutes"
            )
        if (
            isinstance(self.planning_random_seed, bool)
            or not isinstance(self.planning_random_seed, Integral)
            or self.planning_random_seed < 0
        ):
            raise ValueError("planning_random_seed must be a nonnegative integer")
        if self.consecutive_trigger_count > self.observation_window_size:
            raise ValueError("consecutive_trigger_count cannot exceed observation_window_size")
        if self.observation_trigger_count > self.observation_window_size:
            raise ValueError("observation_trigger_count cannot exceed observation_window_size")


@dataclass(frozen=True)
class ThresholdSnapshot:
    """一个判断时刻的群控特征和三重阈值快照。"""

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
    observed_reverse_flow_probability: float | None = None
    reverse_flow_probability_valid: bool = True
    reverse_flow_probability_fallback_reason: str = ""
    inputs_valid: bool = True


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


@dataclass(frozen=True)
class GroupControlInput:
    """群控监督器在一个判断时刻所需的输入和可选设备反馈。

    ``measured_curtailment_mw``是当前实测总限发量，提供时覆盖监督器的指令
    累计估计；``achieved_restoration_mw``是最近一次恢复指令的实际响应量，
    仅在恢复驻留期满的评价时刻使用。
    """

    time_minutes: float
    pcc_power_mw: float
    current_net_load_mw: float
    previous_net_load_mw: float
    maximum_load_mw: float
    pv_capacity_mw: float
    reverse_flow_probability: float
    p_grid_max_mw: float
    pv_actual_mw: float = 0.0
    measured_curtailment_mw: float | None = None
    achieved_restoration_mw: float | None = None
    recovery_command_acknowledged: bool = True
    recovery_command_valid: bool = True
    controllable_pv_available: bool = True
    voltage_within_limits: bool = True
    line_capacity_within_limits: bool = True
    power_factor_within_limits: bool = True
    storage_soc_within_limits: bool = True
    recovery_rearm_requested: bool = False
    actuation_execution_known: bool = True


@dataclass(frozen=True)
class GroupControlDecision:
    """群控监督状态机的单步区域级决策契约。"""

    state: GroupControlState
    thresholds: ThresholdSnapshot
    action: GroupControlAction = GroupControlAction.NONE
    required_curtailment_mw: float = 0.0
    requested_curtailment_mw: float = 0.0
    unserved_curtailment_mw: float = 0.0
    requested_restoration_mw: float = 0.0
    trigger_reason: str = ""
    immediate: bool = False
    consecutive_risk_count: int = 0
    observation_window_hits: int = 0
    remaining_curtailment_mw: float = 0.0
    recovery_dwell_remaining_minutes: float = 0.0
    recovery_evaluation_passed: bool | None = None
    restoration_response_error_mw: float = 0.0
    control_output_valid: bool = True
    reverse_flow_probability_valid: bool = True
    hard_protection_active: bool = False


@dataclass(frozen=True)
class GroupControlEvent:
    """用于后续导出和审计的群控状态转换与控制动作事件。"""

    time_minutes: float
    previous_state: GroupControlState
    new_state: GroupControlState
    event_type: str
    reason: str
    requested_power_mw: float = 0.0
    achieved_power_mw: float = 0.0


@dataclass(frozen=True)
class PVStationControlInput:
    """一个光伏场站参与实时限发分配所需的量测和能力边界。"""

    station_name: str
    bus: str
    actual_power_mw: float
    minimum_power_mw: float = 0.0
    ramp_down_mw_per_minute: float | None = None
    is_in_service: bool = True
    is_healthy: bool = True
    communication_available: bool = True
    is_controllable: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.station_name, str) or not self.station_name.strip():
            raise ValueError("station_name must be a non-empty string")
        if not isinstance(self.bus, str) or not self.bus.strip():
            raise ValueError("bus must be a non-empty string")

        numeric_values = {
            "actual_power_mw": self.actual_power_mw,
            "minimum_power_mw": self.minimum_power_mw,
        }
        if self.ramp_down_mw_per_minute is not None:
            numeric_values["ramp_down_mw_per_minute"] = self.ramp_down_mw_per_minute
        for name, value in numeric_values.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not isfinite(float(value))
            ):
                raise ValueError(f"{name} must be a finite real number")
        if self.actual_power_mw < 0.0:
            raise ValueError("actual_power_mw must be nonnegative")
        if self.minimum_power_mw < 0.0:
            raise ValueError("minimum_power_mw must be nonnegative")
        if self.minimum_power_mw > self.actual_power_mw:
            raise ValueError("minimum_power_mw cannot exceed actual_power_mw")
        if (
            self.ramp_down_mw_per_minute is not None
            and self.ramp_down_mw_per_minute < 0.0
        ):
            raise ValueError("ramp_down_mw_per_minute must be nonnegative")

        boolean_values = {
            "is_in_service": self.is_in_service,
            "is_healthy": self.is_healthy,
            "communication_available": self.communication_available,
            "is_controllable": self.is_controllable,
        }
        invalid_booleans = [
            name for name, value in boolean_values.items() if not isinstance(value, bool)
        ]
        if invalid_booleans:
            raise ValueError(
                f"PV station status fields must be booleans: {', '.join(invalid_booleans)}"
            )


@dataclass(frozen=True)
class PVStationCurtailmentCommand:
    """一个场站的比例请求、能力限幅结果和目标有功指令。"""

    station_name: str
    bus: str
    availability: PVStationAvailability
    actual_power_mw: float
    initial_proportional_request_mw: float
    available_curtailment_mw: float
    allocated_curtailment_mw: float
    target_power_mw: float
    saturated: bool
    limiting_constraint: str = ""
    capability_valid: bool = True


@dataclass(frozen=True)
class PVCurtailmentAllocation:
    """一次光伏集群限发任务的逐站分配结果。"""

    requested_curtailment_mw: float
    allocated_curtailment_mw: float
    unserved_curtailment_mw: float
    commands: tuple[PVStationCurtailmentCommand, ...]
    capability_complete: bool = True
    invalid_capability_stations: tuple[str, ...] = ()

    @property
    def fully_allocated(self) -> bool:
        """返回本轮限发需求是否已被场站能力完整承接。"""
        return self.unserved_curtailment_mw <= 1e-9


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
    network_recovery_blocked: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=bool))
    wind_command_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    wind_available_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    wind_actual_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    execution_evidence_version: str | None = None
    wind_resource_actual_mw: Dict[str, np.ndarray] = field(default_factory=dict)
    wind_availability_limited_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pv_availability_limited_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_restoration_evidence: tuple[RestorationEvidence, ...] = ()
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
    storage_reactive_actual_mvar: np.ndarray = field(default_factory=lambda: np.empty(0))
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
    hard_wind_storage_target_mw: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    hard_safety_unserved_mw: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    hard_wind_storage_command_accepted: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    group_control_state: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=str))
    group_control_action: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=str))
    group_control_reason: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=str))
    group_risk_limit_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_safety_threshold_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_restore_threshold_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_current_net_load_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_load_change_rate: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_pv_penetration: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_reverse_flow_probability: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_observed_reverse_flow_probability: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    group_reverse_flow_probability_valid: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    group_risk_index: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_required_curtailment_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_requested_curtailment_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_unserved_curtailment_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_requested_restoration_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_achieved_restoration_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_remaining_curtailment_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_recovery_dwell_remaining_minutes: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    group_recovery_evaluation_passed: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    group_restoration_response_error_mw: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    measured_pv_curtailment_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    pcc_before_local_safety_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    local_reverse_flow_reduction_mw: np.ndarray = field(default_factory=lambda: np.empty(0))
    group_hard_override_active: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    local_power_factor_adjustment_mvar: np.ndarray = field(
        default_factory=lambda: np.empty(0)
    )
    actual_power_factor: np.ndarray = field(default_factory=lambda: np.empty(0))
    storage_energy_mwh: np.ndarray = field(default_factory=lambda: np.empty(0))
    storage_dynamics: tuple[StorageDynamicsRecord, ...] = ()
    storage_soc_within_limits: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=bool)
    )
    group_control_events: tuple[GroupControlEvent, ...] = ()
    group_curtailment_commands: int = 0
    group_restoration_commands: int = 0
    group_recovery_aborts: int = 0
    local_reverse_flow_interventions: int = 0
    local_power_factor_interventions: int = 0


@dataclass
class HierarchicalResult:
    admm: ADMMResult
    centralized_reference: object
    local_milp_results: Dict[str, object]
    intraday_milp_results: Dict[str, object]
    tracking: Dict[str, DeviceTrackingResult]
    comparison: Dict[str, float | bool]
    centralized_ac_consistency: object | None = None
    local_ac_consistency: Dict[str, object] = field(default_factory=dict)
    intraday_ac_consistency: Dict[str, object] = field(default_factory=dict)
    network_formulation: str = "MISOCP Branch Flow"
    legacy_centralized_reference: object | None = None
    day_ahead_security: Dict[str, PlanningSecurityTrajectory] = field(default_factory=dict)
    intraday_security: Dict[str, PlanningSecurityTrajectory] = field(default_factory=dict)
    cluster_validation: ClusterValidation | None = None

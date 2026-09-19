"""分布式光伏群调群控的特征、动态三重阈值与监督状态机。

阈值计算函数保持无状态和确定性。:class:`GroupControlSupervisor` 在此基础上
实现参考策略要求的紧急触发、3/5防抖、二次调控、限发保持和渐进恢复逻辑，
但不直接下发设备指令；其输出是后续区域MISOCP和设备控制层的稳定契约。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from math import isfinite
from numbers import Real

from .control_contracts import (
    GroupTelemetrySnapshot,
    PVStationSnapshot,
    ResolvedReverseFlowRisk,
)
from .hierarchy_types import (
    GroupControlAction,
    GroupControlConfig,
    GroupControlDecision,
    GroupControlEvent,
    GroupControlInput,
    GroupControlState,
    PVCurtailmentAllocation,
    PVStationAvailability,
    PVStationControlInput,
    PVStationCurtailmentCommand,
    ThresholdSnapshot,
)


_ALLOCATION_TOLERANCE_MW = 1e-10


@dataclass(frozen=True)
class _RecoveryOutcome:
    """监督器内部使用的单周期恢复处理结果。"""

    action: GroupControlAction = GroupControlAction.HOLD
    reason: str = ""
    requested_restoration_mw: float = 0.0
    dwell_remaining_minutes: float = 0.0
    evaluation_passed: bool | None = None
    response_error_mw: float = 0.0
    achieved_power_mw: float = 0.0


@dataclass(frozen=True)
class _NormalizedStepInput:
    """兼容旧输入和生产快照后的单步领域视图。"""

    control_input: GroupControlInput
    aggregate_valid: bool
    invalid_reason: str
    reverse_flow_risk: ResolvedReverseFlowRisk
    hard_protection_active: bool


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
    config: GroupControlConfig,
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
    config: GroupControlConfig | None = None,
) -> float:
    """根据光伏渗透率、负荷变化率和倒送概率计算综合风险指数。"""
    cfg = config or GroupControlConfig()
    return _normalized_risk_features(
        pv_penetration,
        load_change_rate,
        reverse_flow_probability,
        cfg,
    )[2]


def calculate_thresholds(
    control_input: GroupControlInput,
    *,
    config: GroupControlConfig | None = None,
) -> ThresholdSnapshot:
    """计算一个判断时刻的安全阈值、风险限值和恢复阈值。

    阈值功率均按``maximum_load_mw``缩放。若区域PCC最大受电能力不足以
    容纳计算得到的恢复阈值，函数拒绝该配置，而不是静默破坏三重阈值
    的严格顺序。
    """
    cfg = config or GroupControlConfig()
    time_minutes = _require_finite("time_minutes", control_input.time_minutes)
    _require_finite("pcc_power_mw", control_input.pcc_power_mw)
    current_load = _require_finite("current_net_load_mw", control_input.current_net_load_mw)
    previous_load = _require_finite("previous_net_load_mw", control_input.previous_net_load_mw)
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
        cfg.base_safety_margin_ratio
        + cfg.maximum_safety_increment_ratio * risk_index
    )
    risk_buffer = maximum_load * (
        cfg.maximum_risk_buffer_ratio
        - (cfg.maximum_risk_buffer_ratio - cfg.minimum_risk_buffer_ratio) * risk_index
    )
    restore_buffer = maximum_load * (
        cfg.minimum_restore_buffer_ratio
        + (cfg.maximum_restore_buffer_ratio - cfg.minimum_restore_buffer_ratio) * risk_index
    )
    risk_limit = safety_threshold - risk_buffer
    restore_threshold = safety_threshold + restore_buffer

    if not 0.0 < risk_limit < safety_threshold < restore_threshold:
        raise ValueError(
            "group-control configuration does not produce strictly ordered positive thresholds"
        )
    if restore_threshold > p_grid_max:
        raise ValueError(
            "restore threshold exceeds p_grid_max_mw; adjust group-control ratios or area capacity"
        )

    return ThresholdSnapshot(
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
        observed_reverse_flow_probability=probability,
    )


def _normalize_step_input(
    value: GroupControlInput | GroupTelemetrySnapshot,
    config: GroupControlConfig,
) -> _NormalizedStepInput:
    """把生产快照转换为现有算法输入，同时保留真实性和硬保护元数据。"""

    if isinstance(value, GroupControlInput):
        probability = _require_finite(
            "reverse_flow_probability", value.reverse_flow_probability
        )
        return _NormalizedStepInput(
            control_input=value,
            aggregate_valid=True,
            invalid_reason="",
            reverse_flow_risk=ResolvedReverseFlowRisk(
                observed_probability=probability,
                effective_probability=probability,
                valid=True,
            ),
            hard_protection_active=False,
        )
    if not isinstance(value, GroupTelemetrySnapshot):
        raise TypeError("group control step requires GroupControlInput or GroupTelemetrySnapshot")

    risk = value.reverse_flow_risk.resolve(
        decision_time_minutes=value.time_minutes,
        expected_horizon_minutes=config.risk_probability_horizon_minutes,
    )
    invalid_reasons = []
    if not value.coherent:
        invalid_reasons.append("snapshot_incoherent")
    validity_fields = {
        "pcc_telemetry_invalid": value.pcc_validity.valid,
        "load_telemetry_invalid": value.load_validity.valid,
        "pv_aggregate_telemetry_invalid": value.pv_aggregate_validity.valid,
        "network_limits_invalid": value.network_limits_validity.valid,
        "hard_protection_status_invalid": value.hard_protection.validity.valid,
    }
    invalid_reasons.extend(
        reason for reason, valid in validity_fields.items() if not valid
    )
    normalized = GroupControlInput(
        time_minutes=value.time_minutes,
        pcc_power_mw=value.pcc_power_mw,
        current_net_load_mw=value.current_net_load_mw,
        previous_net_load_mw=value.previous_net_load_mw,
        maximum_load_mw=value.maximum_load_mw,
        pv_capacity_mw=value.pv_capacity_mw,
        reverse_flow_probability=risk.effective_probability,
        p_grid_max_mw=value.p_grid_max_mw,
        pv_actual_mw=value.pv_actual_mw,
        measured_curtailment_mw=value.measured_supervisor_curtailment_mw,
        achieved_restoration_mw=value.achieved_restoration_mw,
        recovery_command_acknowledged=value.recovery_command_acknowledged,
        recovery_command_valid=value.recovery_command_valid,
        controllable_pv_available=value.controllable_pv_available,
        voltage_within_limits=value.voltage_within_limits,
        line_capacity_within_limits=value.line_capacity_within_limits,
        power_factor_within_limits=value.power_factor_within_limits,
        storage_soc_within_limits=value.storage_soc_within_limits,
        recovery_rearm_requested=value.recovery_rearm_requested,
        actuation_execution_known=all(
            station.actuation_feedback.execution_known
            for station in value.stations
        ),
    )
    return _NormalizedStepInput(
        control_input=normalized,
        aggregate_valid=not invalid_reasons,
        invalid_reason=invalid_reasons[0] if invalid_reasons else "",
        reverse_flow_risk=risk,
        hard_protection_active=value.hard_protection.active,
    )


def _station_availability(station: PVStationControlInput) -> PVStationAvailability:
    """按确定的优先级返回场站不可用原因。"""
    if not station.is_in_service:
        return PVStationAvailability.OUT_OF_SERVICE
    if not station.is_healthy:
        return PVStationAvailability.FAULTED
    if not station.communication_available:
        return PVStationAvailability.COMMUNICATION_LOST
    if not station.is_controllable:
        return PVStationAvailability.UNCONTROLLABLE
    return PVStationAvailability.AVAILABLE


def _station_curtailment_capacity(
    station: PVStationControlInput,
    decision_interval_minutes: float,
) -> tuple[float, str]:
    """计算单个可用场站在本判定周期内的最大可执行限发量。"""
    headroom = float(station.actual_power_mw - station.minimum_power_mw)
    if station.ramp_down_mw_per_minute is None:
        return headroom, "minimum_power_limit"
    ramp_limit = float(station.ramp_down_mw_per_minute) * decision_interval_minutes
    if ramp_limit < headroom - _ALLOCATION_TOLERANCE_MW:
        return ramp_limit, "ramp_down_limit"
    return headroom, "minimum_power_limit"


def allocate_pv_curtailment(
    requested_curtailment_mw: float,
    stations: Iterable[PVStationControlInput | PVStationSnapshot],
    *,
    decision_interval_minutes: float = 1.0,
) -> PVCurtailmentAllocation:
    """按实时出力比例分配限发任务，并迭代重分配饱和后的剩余量。

    只有运行、健康、通信在线且可控的光伏场站参与分配。每个场站的最终指令
    同时受最小有功和本判定周期下调爬坡能力限制。返回顺序与输入顺序一致，
    便于调用方与SCADA点表进行稳定映射。
    """
    requested = _require_finite("requested_curtailment_mw", requested_curtailment_mw)
    interval = _require_finite("decision_interval_minutes", decision_interval_minutes)
    if requested < 0.0:
        raise ValueError("requested_curtailment_mw must be nonnegative")
    if interval <= 0.0:
        raise ValueError("decision_interval_minutes must be positive")

    raw_stations = tuple(stations)
    normalized_stations = []
    availability_overrides: list[PVStationAvailability | None] = []
    for station in raw_stations:
        if isinstance(station, PVStationSnapshot):
            if not station.controllable_measurements_valid:
                availability_overrides.append(
                    PVStationAvailability.TELEMETRY_INVALID
                )
            elif not station.actuation_feedback.execution_known:
                availability_overrides.append(
                    PVStationAvailability.EXECUTION_UNKNOWN
                )
            else:
                availability_overrides.append(None)
            normalized_stations.append(PVStationControlInput(
                station_name=station.station_id,
                bus=station.bus,
                actual_power_mw=station.measured_power_mw,
                minimum_power_mw=min(
                    station.minimum_power_mw, station.measured_power_mw
                ),
                ramp_down_mw_per_minute=station.ramp_down_mw_per_minute,
                is_in_service=station.in_service,
                is_healthy=station.healthy,
                communication_available=station.communication_ok,
                is_controllable=station.controllable,
            ))
        else:
            availability_overrides.append(None)
            normalized_stations.append(station)
    station_list = tuple(normalized_stations)
    invalid_stations = [
        index
        for index, station in enumerate(station_list)
        if not isinstance(station, PVStationControlInput)
    ]
    if invalid_stations:
        raise TypeError(
            "stations must contain only PVStationControlInput instances; "
            f"invalid indices: {invalid_stations}"
        )
    names = [station.station_name for station in station_list]
    if len(set(names)) != len(names):
        raise ValueError("PV station names must be unique within an allocation request")

    availability = [
        override or _station_availability(station)
        for station, override in zip(
            station_list, availability_overrides, strict=True
        )
    ]
    capacities = [0.0] * len(station_list)
    limiting_constraints = [""] * len(station_list)
    for index, station in enumerate(station_list):
        if availability[index] is PVStationAvailability.AVAILABLE:
            capacities[index], limiting_constraints[index] = (
                _station_curtailment_capacity(station, interval)
            )

    participating = [
        index
        for index, station in enumerate(station_list)
        if availability[index] is PVStationAvailability.AVAILABLE
        and station.actual_power_mw > _ALLOCATION_TOLERANCE_MW
    ]
    participating_output = sum(
        float(station_list[index].actual_power_mw) for index in participating
    )
    initial_requests = [0.0] * len(station_list)
    if requested > 0.0 and participating_output > _ALLOCATION_TOLERANCE_MW:
        for index in participating:
            initial_requests[index] = (
                requested
                * float(station_list[index].actual_power_mw)
                / participating_output
            )

    allocations = [0.0] * len(station_list)
    remaining_request = requested
    active = [
        index
        for index in participating
        if capacities[index] > _ALLOCATION_TOLERANCE_MW
    ]
    while active and remaining_request > _ALLOCATION_TOLERANCE_MW:
        active_output = sum(
            float(station_list[index].actual_power_mw) for index in active
        )
        proportional_shares = {
            index: (
                remaining_request
                * float(station_list[index].actual_power_mw)
                / active_output
            )
            for index in active
        }
        saturated = [
            index
            for index in active
            if proportional_shares[index]
            >= capacities[index] - allocations[index]
        ]
        if not saturated:
            for index in active:
                allocations[index] += proportional_shares[index]
            remaining_request = 0.0
            break

        for index in saturated:
            incremental = capacities[index] - allocations[index]
            allocations[index] = capacities[index]
            remaining_request = max(0.0, remaining_request - incremental)
        saturated_indices = set(saturated)
        active = [index for index in active if index not in saturated_indices]

    allocated_total = sum(allocations)
    unserved = max(0.0, requested - allocated_total)
    commands = []
    for index, station in enumerate(station_list):
        is_saturated = (
            availability[index] is PVStationAvailability.AVAILABLE
            and requested > _ALLOCATION_TOLERANCE_MW
            and capacities[index] <= allocations[index] + _ALLOCATION_TOLERANCE_MW
        )
        commands.append(
            PVStationCurtailmentCommand(
                station_name=station.station_name,
                bus=station.bus,
                availability=availability[index],
                actual_power_mw=float(station.actual_power_mw),
                initial_proportional_request_mw=initial_requests[index],
                available_curtailment_mw=capacities[index],
                allocated_curtailment_mw=allocations[index],
                target_power_mw=max(
                    float(station.minimum_power_mw),
                    float(station.actual_power_mw) - allocations[index],
                ),
                saturated=is_saturated,
                limiting_constraint=(
                    limiting_constraints[index] if is_saturated else ""
                ),
                capability_valid=availability[index] not in {
                    PVStationAvailability.TELEMETRY_INVALID,
                    PVStationAvailability.EXECUTION_UNKNOWN,
                },
            )
        )

    return PVCurtailmentAllocation(
        requested_curtailment_mw=requested,
        allocated_curtailment_mw=allocated_total,
        unserved_curtailment_mw=unserved,
        commands=tuple(commands),
        capability_complete=all(
            command.capability_valid for command in commands
        ),
        invalid_capability_stations=tuple(
            command.station_name
            for command in commands
            if not command.capability_valid
        ),
    )


class GroupControlSupervisor:
    """按固定判定周期运行的限发与渐进恢复群控监督状态机。

    状态机只生成区域级有功调整需求，不修改优化模型或设备状态。调用方必须按
    时间顺序传入观测；若观测间隔大于配置的判定周期，监督器会清空3/5判据的
    历史证据。恢复驻留期间发生观测中断时会中止恢复，因为无法证明安全条件
    在整个驻留期内持续成立。
    """

    _TIME_TOLERANCE_MINUTES = 1e-9
    _POWER_TOLERANCE_MW = 1e-9

    def __init__(self, config: GroupControlConfig | None = None) -> None:
        self.config = config or GroupControlConfig()
        self.reset()

    @property
    def state(self) -> GroupControlState:
        """返回当前离散状态。"""
        return self._state

    @property
    def curtailment_active(self) -> bool:
        """返回监督器是否已经进入限发控制阶段。"""
        return self._curtailment_active

    @property
    def remaining_curtailment_mw(self) -> float:
        """返回尚待恢复的光伏限发功率。"""
        return self._remaining_curtailment_mw

    @property
    def recovery_inhibited(self) -> bool:
        """返回恢复是否因执行可信度或网络安全失败而锁存禁止。"""

        return bool(self._recovery_inhibit_reason)

    @property
    def events(self) -> tuple[GroupControlEvent, ...]:
        """以不可变快照形式返回状态转换和限发指令审计记录。"""
        return tuple(self._events)

    def reset(self) -> None:
        """清除运行状态、判据历史和审计事件。"""
        self._state = GroupControlState.NORMAL
        self._curtailment_active = False
        self._risk_observations: deque[bool] = deque(
            maxlen=self.config.observation_window_size
        )
        self._consecutive_risk_count = 0
        self._last_time_minutes: float | None = None
        self._remaining_curtailment_mw = 0.0
        self._recovery_step_reference_mw = 0.0
        self._pending_restoration_mw = 0.0
        self._restoration_command_time_minutes: float | None = None
        self._recovery_inhibit_reason = ""
        self._events: list[GroupControlEvent] = []

    def _accept_timestamp(self, time_minutes: float) -> bool:
        """校验时间顺序并返回本次观测前是否发生采样中断。"""
        if self._last_time_minutes is None:
            self._last_time_minutes = time_minutes
            return False

        elapsed = time_minutes - self._last_time_minutes
        expected = float(self.config.decision_interval_minutes)
        if elapsed <= self._TIME_TOLERANCE_MINUTES:
            raise ValueError("group-control timestamps must be strictly increasing")
        if elapsed < expected - self._TIME_TOLERANCE_MINUTES:
            raise ValueError(
                "group-control observations cannot be more frequent than "
                "decision_interval_minutes"
            )
        if elapsed > expected + self._TIME_TOLERANCE_MINUTES:
            self._risk_observations.clear()
            self._consecutive_risk_count = 0
            sampling_gap = True
        else:
            sampling_gap = False
        self._last_time_minutes = time_minutes
        return sampling_gap

    def _validate_recovery_feedback(
        self,
        control_input: GroupControlInput,
    ) -> tuple[float | None, float | None]:
        """校验可选量测和恢复安全状态，不在失败时修改运行状态。"""
        measured_curtailment = control_input.measured_curtailment_mw
        if measured_curtailment is not None:
            measured_curtailment = _require_finite(
                "measured_curtailment_mw", measured_curtailment
            )
            if measured_curtailment < 0.0:
                raise ValueError("measured_curtailment_mw must be nonnegative")
            if measured_curtailment > control_input.pv_capacity_mw:
                raise ValueError("measured_curtailment_mw cannot exceed pv_capacity_mw")

        achieved_restoration = control_input.achieved_restoration_mw
        if achieved_restoration is not None:
            achieved_restoration = _require_finite(
                "achieved_restoration_mw", achieved_restoration
            )
            if achieved_restoration < 0.0:
                raise ValueError("achieved_restoration_mw must be nonnegative")

        boolean_values = {
            "recovery_command_acknowledged": (
                control_input.recovery_command_acknowledged
            ),
            "recovery_command_valid": control_input.recovery_command_valid,
            "controllable_pv_available": control_input.controllable_pv_available,
            "voltage_within_limits": control_input.voltage_within_limits,
            "line_capacity_within_limits": (
                control_input.line_capacity_within_limits
            ),
            "power_factor_within_limits": (
                control_input.power_factor_within_limits
            ),
            "storage_soc_within_limits": control_input.storage_soc_within_limits,
            "recovery_rearm_requested": control_input.recovery_rearm_requested,
            "actuation_execution_known": control_input.actuation_execution_known,
        }
        invalid_booleans = [
            name for name, value in boolean_values.items() if not isinstance(value, bool)
        ]
        if invalid_booleans:
            raise ValueError(
                "recovery status fields must be booleans: "
                f"{', '.join(invalid_booleans)}"
            )
        return measured_curtailment, achieved_restoration

    @staticmethod
    def _recovery_failure_reason(
        control_input: GroupControlInput,
        thresholds: ThresholdSnapshot,
    ) -> str | None:
        """返回会阻止或中止渐进恢复的首个安全失败原因。"""
        if not control_input.actuation_execution_known:
            return "actuation_execution_unknown"
        if not control_input.recovery_command_acknowledged:
            return "recovery_command_not_acknowledged"
        if not control_input.recovery_command_valid:
            return "recovery_command_invalid_or_expired"
        if not control_input.controllable_pv_available:
            return "controllable_pv_unavailable"
        if not control_input.voltage_within_limits:
            return "voltage_limit_violation"
        if not control_input.line_capacity_within_limits:
            return "line_capacity_violation"
        if not control_input.power_factor_within_limits:
            return "power_factor_violation"
        if not control_input.storage_soc_within_limits:
            return "storage_soc_violation"
        return None

    def _clear_pending_restoration(self) -> None:
        self._pending_restoration_mw = 0.0
        self._restoration_command_time_minutes = None

    def _abort_recovery(
        self,
        reason: str,
        *,
        response_error_mw: float = 0.0,
        achieved_power_mw: float = 0.0,
    ) -> _RecoveryOutcome:
        """中止当前恢复步骤并保留尚待恢复的实际限发量。"""
        self._state = GroupControlState.RECOVERY_ABORTED
        self._recovery_inhibit_reason = reason
        self._clear_pending_restoration()
        return _RecoveryOutcome(
            action=GroupControlAction.ABORT_RECOVERY,
            reason=reason,
            evaluation_passed=False,
            response_error_mw=response_error_mw,
            achieved_power_mw=achieved_power_mw,
        )

    def _handle_restoring(
        self,
        control_input: GroupControlInput,
        thresholds: ThresholdSnapshot,
        *,
        time_minutes: float,
        sampling_gap: bool,
        measured_curtailment: float | None,
        achieved_restoration: float | None,
    ) -> _RecoveryOutcome:
        """执行恢复驻留监测，并在驻留期满时评价设备响应。"""
        if control_input.pcc_power_mw < thresholds.safety_threshold_mw:
            # 可信量测下安全余量暂时不足属于正常控制条件变化，不锁存为执行故障。
            self._state = GroupControlState.CURTAILED_HOLD
            self._clear_pending_restoration()
            return _RecoveryOutcome(
                reason="recovery_safety_margin_lost",
                evaluation_passed=False,
            )
        failure_reason = self._recovery_failure_reason(control_input, thresholds)
        if sampling_gap:
            failure_reason = "recovery_monitoring_gap"
        if failure_reason is not None:
            return self._abort_recovery(failure_reason)
        if self._restoration_command_time_minutes is None:
            return self._abort_recovery("missing_restoration_command_timestamp")

        elapsed = time_minutes - self._restoration_command_time_minutes
        dwell_remaining = max(
            0.0,
            float(self.config.recovery_pause_minutes) - elapsed,
        )
        if dwell_remaining > self._TIME_TOLERANCE_MINUTES:
            self._state = GroupControlState.RESTORING
            return _RecoveryOutcome(
                reason="recovery_dwell_monitoring",
                dwell_remaining_minutes=dwell_remaining,
            )
        if achieved_restoration is None:
            return self._abort_recovery("missing_restoration_feedback")

        response_error = abs(
            achieved_restoration - self._pending_restoration_mw
        )
        response_limit = (
            self.config.response_relative_tolerance
            * self._pending_restoration_mw
        )
        if measured_curtailment is None:
            self._remaining_curtailment_mw = max(
                0.0,
                self._remaining_curtailment_mw - achieved_restoration,
            )
        if response_error > response_limit:
            return self._abort_recovery(
                "restoration_response_mismatch",
                response_error_mw=response_error,
                achieved_power_mw=achieved_restoration,
            )

        self._clear_pending_restoration()
        if self._remaining_curtailment_mw <= self._POWER_TOLERANCE_MW:
            self._remaining_curtailment_mw = 0.0
            self._recovery_step_reference_mw = 0.0
            self._curtailment_active = False
            self._recovery_inhibit_reason = ""
            self._state = GroupControlState.NORMAL
            reason = "recovery_completed"
        elif control_input.pcc_power_mw > thresholds.restore_threshold_mw:
            self._state = GroupControlState.RESTORE_WAIT
            reason = "recovery_step_accepted"
        else:
            self._state = GroupControlState.CURTAILED_HOLD
            reason = "recovery_step_accepted_hold"
        return _RecoveryOutcome(
            reason=reason,
            evaluation_passed=True,
            response_error_mw=response_error,
            achieved_power_mw=achieved_restoration,
        )

    def _handle_restore_wait(
        self,
        control_input: GroupControlInput,
        thresholds: ThresholdSnapshot,
        *,
        time_minutes: float,
    ) -> _RecoveryOutcome:
        """复核恢复准入条件，并在满足条件时发出一个恢复增量。"""
        if control_input.pcc_power_mw <= thresholds.restore_threshold_mw:
            self._state = GroupControlState.CURTAILED_HOLD
            return _RecoveryOutcome(reason="restore_threshold_not_maintained")

        failure_reason = self._recovery_failure_reason(control_input, thresholds)
        if failure_reason is not None:
            return self._abort_recovery(failure_reason)
        if self._remaining_curtailment_mw <= self._POWER_TOLERANCE_MW:
            self._remaining_curtailment_mw = 0.0
            self._recovery_step_reference_mw = 0.0
            self._curtailment_active = False
            self._recovery_inhibit_reason = ""
            self._state = GroupControlState.NORMAL
            return _RecoveryOutcome(reason="recovery_completed")

        self._recovery_step_reference_mw = max(
            self._recovery_step_reference_mw,
            self._remaining_curtailment_mw,
        )
        requested_restoration = min(
            self._remaining_curtailment_mw,
            self.config.recovery_step_fraction * self._recovery_step_reference_mw,
        )
        self._pending_restoration_mw = requested_restoration
        self._restoration_command_time_minutes = time_minutes
        self._state = GroupControlState.RESTORING
        return _RecoveryOutcome(
            action=GroupControlAction.RESTORE,
            reason="progressive_restoration_step",
            requested_restoration_mw=requested_restoration,
            dwell_remaining_minutes=float(self.config.recovery_pause_minutes),
        )

    def _handle_aborted_recovery(
        self,
        control_input: GroupControlInput,
        thresholds: ThresholdSnapshot,
    ) -> _RecoveryOutcome:
        """把一次中止事件转入显式复归锁存，绝不因 blocker 消失自动重试。"""

        self._state = GroupControlState.RECOVERY_INHIBIT
        return self._handle_recovery_inhibit(control_input, thresholds)

    def _handle_recovery_inhibit(
        self,
        control_input: GroupControlInput,
        thresholds: ThresholdSnapshot,
    ) -> _RecoveryOutcome:
        """锁存恢复禁止；只接受显式复归，并至少再等待一个可信周期。"""

        if not control_input.recovery_rearm_requested:
            self._state = GroupControlState.RECOVERY_INHIBIT
            return _RecoveryOutcome(
                reason=self._recovery_inhibit_reason or "recovery_rearm_required"
            )
        failure_reason = self._recovery_failure_reason(control_input, thresholds)
        if failure_reason is not None:
            self._state = GroupControlState.RECOVERY_INHIBIT
            return _RecoveryOutcome(reason=f"recovery_rearm_blocked:{failure_reason}")
        self._recovery_inhibit_reason = ""
        self._clear_pending_restoration()
        self._state = GroupControlState.CURTAILED_HOLD
        return _RecoveryOutcome(reason="recovery_rearmed")

    def _handle_curtailment_hold(
        self,
        control_input: GroupControlInput,
        thresholds: ThresholdSnapshot,
    ) -> _RecoveryOutcome:
        """保持限发，或在迟滞条件满足后进入恢复准备状态。"""
        if self._remaining_curtailment_mw <= self._POWER_TOLERANCE_MW:
            self._remaining_curtailment_mw = 0.0
            self._recovery_step_reference_mw = 0.0
            self._curtailment_active = False
            self._recovery_inhibit_reason = ""
            if control_input.pcc_power_mw < thresholds.safety_threshold_mw:
                self._state = GroupControlState.PREPARED
            else:
                self._state = GroupControlState.NORMAL
            return _RecoveryOutcome(
                action=GroupControlAction.NONE,
                reason="no_curtailment_to_restore",
            )
        if control_input.pcc_power_mw > thresholds.restore_threshold_mw:
            self._recovery_step_reference_mw = self._remaining_curtailment_mw
            self._state = GroupControlState.RESTORE_WAIT
            return _RecoveryOutcome(reason="restore_threshold_reached")
        self._state = GroupControlState.CURTAILED_HOLD
        return _RecoveryOutcome(reason="curtailment_hold")

    def _handle_active_curtailment(
        self,
        control_input: GroupControlInput,
        thresholds: ThresholdSnapshot,
        *,
        previous_state: GroupControlState,
        time_minutes: float,
        sampling_gap: bool,
        measured_curtailment: float | None,
        achieved_restoration: float | None,
    ) -> _RecoveryOutcome:
        """将限发后的状态分派给相应恢复阶段处理器。"""
        if previous_state is GroupControlState.RESTORING:
            return self._handle_restoring(
                control_input,
                thresholds,
                time_minutes=time_minutes,
                sampling_gap=sampling_gap,
                measured_curtailment=measured_curtailment,
                achieved_restoration=achieved_restoration,
            )
        if previous_state is GroupControlState.RESTORE_WAIT:
            return self._handle_restore_wait(
                control_input,
                thresholds,
                time_minutes=time_minutes,
            )
        if previous_state is GroupControlState.RECOVERY_ABORTED:
            return self._handle_aborted_recovery(control_input, thresholds)
        if (
            previous_state is GroupControlState.RECOVERY_INHIBIT
            or self._recovery_inhibit_reason
        ):
            return self._handle_recovery_inhibit(control_input, thresholds)
        return self._handle_curtailment_hold(control_input, thresholds)

    def _risk_trigger(self, thresholds: ThresholdSnapshot) -> tuple[str, bool] | None:
        """按优先级返回本周期的限发触发原因与紧急标志。"""
        if self._curtailment_active:
            return "secondary_control", True
        if abs(thresholds.load_change_rate) > self.config.emergency_load_change_rate:
            return "emergency_load_change_rate", True
        if self._consecutive_risk_count >= self.config.consecutive_trigger_count:
            return "consecutive_risk_limit", False
        if (
            len(self._risk_observations) == self.config.observation_window_size
            and sum(self._risk_observations) >= self.config.observation_trigger_count
        ):
            return "frequency_risk_limit", False
        return None

    def _record_event(
        self,
        *,
        time_minutes: float,
        previous_state: GroupControlState,
        new_state: GroupControlState,
        action: GroupControlAction,
        reason: str,
        requested_power_mw: float,
        achieved_power_mw: float,
    ) -> None:
        """记录有审计价值的状态转换或控制动作。"""
        if previous_state == new_state and action in {
            GroupControlAction.NONE,
            GroupControlAction.HOLD,
        }:
            return
        event_types = {
            GroupControlAction.CURTAIL: "curtailment_command",
            GroupControlAction.RESTORE: "restoration_command",
            GroupControlAction.ABORT_RECOVERY: "recovery_abort",
        }
        event_type = event_types.get(action, "state_transition")
        self._events.append(
            GroupControlEvent(
                time_minutes=time_minutes,
                previous_state=previous_state,
                new_state=new_state,
                event_type=event_type,
                reason=reason,
                requested_power_mw=requested_power_mw,
                achieved_power_mw=achieved_power_mw,
            )
        )

    def hold_for_network(self, reason: str, remaining_curtailment_mw: float) -> None:
        """Accept an executed network cap without releasing it or replaying a step.

        This also invalidates an in-flight recovery command. The ordinary explicit
        rearm state machine remains responsible for subsequent restoration.
        """
        remaining = _require_finite("remaining_curtailment_mw", remaining_curtailment_mw)
        if remaining < 0:
            raise ValueError("remaining curtailment must be nonnegative")
        self._clear_pending_restoration()
        self._remaining_curtailment_mw = remaining
        self._curtailment_active = remaining > self._POWER_TOLERANCE_MW
        self._recovery_inhibit_reason = reason
        self._state = GroupControlState.RECOVERY_INHIBIT

    def step(
        self,
        control_input: GroupControlInput | GroupTelemetrySnapshot,
    ) -> GroupControlDecision:
        """处理一个判定周期，并返回不含设备副作用的区域级控制决策。

        新的生产边界应传入 :class:`GroupTelemetrySnapshot`。旧
        :class:`GroupControlInput`仅为已有仿真和测试保留兼容入口。
        """

        normalized = _normalize_step_input(control_input, self.config)
        control_input = normalized.control_input
        thresholds = calculate_thresholds(control_input, config=self.config)
        thresholds = replace(
            thresholds,
            observed_reverse_flow_probability=(
                normalized.reverse_flow_risk.observed_probability
            ),
            reverse_flow_probability_valid=normalized.reverse_flow_risk.valid,
            reverse_flow_probability_fallback_reason=(
                normalized.reverse_flow_risk.fallback_reason
            ),
            inputs_valid=normalized.aggregate_valid,
        )
        pv_actual_mw = _require_finite("pv_actual_mw", control_input.pv_actual_mw)
        if pv_actual_mw < 0.0:
            raise ValueError("pv_actual_mw must be nonnegative")
        measured_curtailment, achieved_restoration = (
            self._validate_recovery_feedback(control_input)
        )

        time_minutes = float(control_input.time_minutes)
        sampling_gap = self._accept_timestamp(time_minutes)
        previous_state = self._state
        pending_restoration_at_start = self._pending_restoration_mw
        if measured_curtailment is not None:
            self._remaining_curtailment_mw = measured_curtailment

        action = GroupControlAction.NONE
        required_curtailment_mw = 0.0
        requested_curtailment_mw = 0.0
        unserved_curtailment_mw = 0.0
        requested_restoration_mw = 0.0
        dwell_remaining_minutes = 0.0
        recovery_evaluation_passed: bool | None = None
        restoration_response_error_mw = 0.0
        achieved_event_power_mw = 0.0
        trigger_reason = ""
        immediate = False
        control_output_valid = normalized.aggregate_valid

        if not normalized.aggregate_valid:
            self._state = GroupControlState.OUTPUT_BLOCK
            self._risk_observations.clear()
            self._consecutive_risk_count = 0
            self._clear_pending_restoration()
            trigger_reason = normalized.invalid_reason or "group_snapshot_invalid"
        elif normalized.hard_protection_active:
            self._state = GroupControlState.HARD_OVERRIDE
            self._risk_observations.clear()
            self._consecutive_risk_count = 0
            self._clear_pending_restoration()
            action = GroupControlAction.HOLD
            trigger_reason = "hard_protection_override"
        else:
            below_risk_limit = control_input.pcc_power_mw < thresholds.risk_limit_mw
            self._risk_observations.append(below_risk_limit)
            if below_risk_limit:
                self._consecutive_risk_count += 1
            else:
                self._consecutive_risk_count = 0

            if previous_state is GroupControlState.HARD_OVERRIDE and not below_risk_limit:
                # 硬保护退出后的第一张可信快照只用于交接确认，不在同周期恢复。
                self._state = (
                    GroupControlState.CURTAILED_HOLD
                    if self._curtailment_active
                    else GroupControlState.NORMAL
                )
                action = (
                    GroupControlAction.HOLD
                    if self._curtailment_active
                    else GroupControlAction.NONE
                )
                trigger_reason = "hard_override_cleared_guard"
            elif previous_state is GroupControlState.OUTPUT_BLOCK and not below_risk_limit:
                if self._curtailment_active:
                    self._recovery_inhibit_reason = (
                        self._recovery_inhibit_reason
                        or "output_block_recovered_rearm_required"
                    )
                    self._state = GroupControlState.RECOVERY_INHIBIT
                    action = GroupControlAction.HOLD
                    trigger_reason = self._recovery_inhibit_reason
                else:
                    self._state = GroupControlState.NORMAL
                    trigger_reason = "output_block_recovered"
            elif below_risk_limit:
                trigger = self._risk_trigger(thresholds)
                if trigger is None:
                    self._state = GroupControlState.RISK_OBSERVING
                    trigger_reason = "risk_limit_observation"
                else:
                    trigger_reason, immediate = trigger
                    required_curtailment_mw = max(
                        0.0,
                        thresholds.safety_threshold_mw
                        - float(control_input.pcc_power_mw),
                    )
                    requested_curtailment_mw = min(
                        required_curtailment_mw, pv_actual_mw
                    )
                    unserved_curtailment_mw = max(
                        0.0,
                        required_curtailment_mw - requested_curtailment_mw,
                    )
                    action = GroupControlAction.CURTAIL
                    self._state = GroupControlState.CURTAILING
                    self._curtailment_active = True
                    self._remaining_curtailment_mw += requested_curtailment_mw
                    # 二次限发使旧恢复上下文作废，下次恢复重新建立 reference。
                    self._recovery_step_reference_mw = 0.0
                    self._clear_pending_restoration()
            elif self._curtailment_active and not normalized.reverse_flow_risk.valid:
                self._recovery_inhibit_reason = (
                    normalized.reverse_flow_risk.fallback_reason
                    or "reverse_flow_probability_invalid"
                )
                self._state = GroupControlState.RECOVERY_INHIBIT
                self._clear_pending_restoration()
                action = GroupControlAction.HOLD
                trigger_reason = self._recovery_inhibit_reason
            elif self._curtailment_active:
                recovery = self._handle_active_curtailment(
                    control_input,
                    thresholds,
                    previous_state=previous_state,
                    time_minutes=time_minutes,
                    sampling_gap=sampling_gap,
                    measured_curtailment=measured_curtailment,
                    achieved_restoration=achieved_restoration,
                )
                action = recovery.action
                trigger_reason = recovery.reason
                requested_restoration_mw = recovery.requested_restoration_mw
                dwell_remaining_minutes = recovery.dwell_remaining_minutes
                recovery_evaluation_passed = recovery.evaluation_passed
                restoration_response_error_mw = recovery.response_error_mw
                achieved_event_power_mw = recovery.achieved_power_mw
            elif control_input.pcc_power_mw < thresholds.safety_threshold_mw:
                self._state = GroupControlState.PREPARED
                trigger_reason = "entered_prepared_zone"
            else:
                self._state = GroupControlState.NORMAL
                trigger_reason = (
                    normalized.reverse_flow_risk.fallback_reason
                    if not normalized.reverse_flow_risk.valid
                    else "normal_operation"
                )

        # 静态类型和审计逻辑均从同一位置读取最终窗口命中数。
        window_hits = sum(self._risk_observations)
        decision = GroupControlDecision(
            state=self._state,
            thresholds=thresholds,
            action=action,
            required_curtailment_mw=required_curtailment_mw,
            requested_curtailment_mw=requested_curtailment_mw,
            unserved_curtailment_mw=unserved_curtailment_mw,
            requested_restoration_mw=requested_restoration_mw,
            trigger_reason=trigger_reason,
            immediate=immediate,
            consecutive_risk_count=self._consecutive_risk_count,
            observation_window_hits=window_hits,
            remaining_curtailment_mw=self._remaining_curtailment_mw,
            recovery_dwell_remaining_minutes=dwell_remaining_minutes,
            recovery_evaluation_passed=recovery_evaluation_passed,
            restoration_response_error_mw=restoration_response_error_mw,
            control_output_valid=control_output_valid,
            reverse_flow_probability_valid=normalized.reverse_flow_risk.valid,
            hard_protection_active=normalized.hard_protection_active,
        )
        self._record_event(
            time_minutes=time_minutes,
            previous_state=previous_state,
            new_state=self._state,
            action=action,
            reason=trigger_reason,
            requested_power_mw=(
                requested_curtailment_mw
                if action is GroupControlAction.CURTAIL
                else (
                    requested_restoration_mw
                    if action is GroupControlAction.RESTORE
                    else (
                        pending_restoration_at_start
                        if recovery_evaluation_passed is not None
                        else 0.0
                    )
                )
            ),
            achieved_power_mw=achieved_event_power_mw,
        )
        return decision


__all__ = [
    "allocate_pv_curtailment",
    "calculate_load_change_rate",
    "calculate_pv_penetration",
    "calculate_risk_index",
    "calculate_thresholds",
    "GroupControlSupervisor",
]

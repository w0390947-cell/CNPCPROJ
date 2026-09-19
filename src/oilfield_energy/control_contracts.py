"""群控生产边界使用的数据可信度、控制意图和现场反馈契约。

本模块不解释任何 D5000/SCADA 原始质量码，也不直接写设备。Adapter 负责把
现场时标和质量码归一化为 :class:`SignalValidity`；domain-core 只消费已经判定
的一致快照，并向唯一执行仲裁器提交带所有权的绝对功率上限意图。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from numbers import Real


def _finite_real(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    converted = float(value)
    if not isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


@dataclass(frozen=True)
class SignalValidity:
    """Adapter 已归一化的单个信号质量；core 不自行猜测过期阈值。"""

    quality_good: bool
    fresh: bool

    def __post_init__(self) -> None:
        if not isinstance(self.quality_good, bool) or not isinstance(self.fresh, bool):
            raise ValueError("signal validity fields must be booleans")

    @property
    def valid(self) -> bool:
        return self.quality_good and self.fresh

    @classmethod
    def valid_signal(cls) -> "SignalValidity":
        """供显式 adapter/stub 构造可信信号，避免生产接口隐式默认真。"""

        return cls(quality_good=True, fresh=True)

    @classmethod
    def invalid_signal(cls) -> "SignalValidity":
        return cls(quality_good=False, fresh=False)


@dataclass(frozen=True)
class ResolvedReverseFlowRisk:
    """一次控制决策实际使用的概率及其真实性元数据。"""

    observed_probability: float | None
    effective_probability: float
    valid: bool
    fallback_reason: str = ""


@dataclass(frozen=True)
class ReverseFlowRiskEstimate:
    """滚动倒送概率预测的领域契约。

    ``probability``表示未来``horizon_minutes``内任一时刻发生倒送的概率。
    ``forecast_as_of_minutes``和``valid_until_minutes``使用与控制快照一致的仿真
    或单调时间轴；真实 UTC/SCADA 时标由 adapter 在进入 core 前完成换算。
    """

    probability: float | None
    forecast_as_of_minutes: float
    horizon_minutes: int
    valid_until_minutes: float
    validity: SignalValidity

    def __post_init__(self) -> None:
        as_of = _finite_real("forecast_as_of_minutes", self.forecast_as_of_minutes)
        valid_until = _finite_real("valid_until_minutes", self.valid_until_minutes)
        if as_of < 0.0:
            raise ValueError("forecast_as_of_minutes must be nonnegative")
        if valid_until < as_of:
            raise ValueError("valid_until_minutes cannot precede forecast_as_of_minutes")
        if isinstance(self.horizon_minutes, bool) or not isinstance(self.horizon_minutes, int):
            raise ValueError("horizon_minutes must be an integer")
        if self.horizon_minutes <= 0:
            raise ValueError("horizon_minutes must be positive")
        if self.probability is not None:
            probability = _finite_real("probability", self.probability)
            if not 0.0 <= probability <= 1.0:
                raise ValueError("probability must be in [0, 1]")

    def resolve(
        self,
        *,
        decision_time_minutes: float,
        expected_horizon_minutes: int,
    ) -> ResolvedReverseFlowRisk:
        """解析预测；无效时显式返回最保守阈值输入而不伪造观测值。"""

        decision_time = _finite_real("decision_time_minutes", decision_time_minutes)
        if self.probability is None:
            reason = "reverse_flow_probability_missing"
        elif not self.validity.valid:
            reason = "reverse_flow_probability_bad_or_stale"
        elif self.forecast_as_of_minutes > decision_time:
            reason = "reverse_flow_probability_future_leakage"
        elif decision_time > self.valid_until_minutes:
            reason = "reverse_flow_probability_expired"
        elif self.horizon_minutes != expected_horizon_minutes:
            reason = "reverse_flow_probability_horizon_mismatch"
        else:
            return ResolvedReverseFlowRisk(
                observed_probability=float(self.probability),
                effective_probability=float(self.probability),
                valid=True,
            )
        return ResolvedReverseFlowRisk(
            observed_probability=self.probability,
            effective_probability=1.0,
            valid=False,
            fallback_reason=reason,
        )


@dataclass(frozen=True)
class StationActuationFeedback:
    """场站 adapter 返回的请求、确认和执行状态，不由 domain 自行推断。"""

    last_requested_command_id: str | None
    last_acknowledged_command_id: str | None
    setpoint_echo_mw: float | None
    execution_known: bool

    def __post_init__(self) -> None:
        if not isinstance(self.execution_known, bool):
            raise ValueError("execution_known must be a boolean")
        for name in ("last_requested_command_id", "last_acknowledged_command_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be None or a non-empty string")
        if self.setpoint_echo_mw is not None:
            if _finite_real("setpoint_echo_mw", self.setpoint_echo_mw) < 0.0:
                raise ValueError("setpoint_echo_mw must be nonnegative")

    @classmethod
    def synthetic_success(cls) -> "StationActuationFeedback":
        """只供确定性仿真显式声明“不要求真实现场 ACK”。"""

        return cls(None, None, None, execution_known=True)


@dataclass(frozen=True)
class PVStationSnapshot:
    """一张一致快照中的单个光伏场站量测、能力和执行反馈。"""

    station_id: str
    bus: str
    measured_power_mw: float
    available_power_mw: float
    minimum_power_mw: float
    ramp_down_mw_per_minute: float | None
    ramp_up_mw_per_minute: float | None
    in_service: bool
    healthy: bool
    communication_ok: bool
    controllable: bool
    measured_power_validity: SignalValidity
    available_power_validity: SignalValidity
    status_validity: SignalValidity
    actuation_feedback: StationActuationFeedback

    def __post_init__(self) -> None:
        if not isinstance(self.station_id, str) or not self.station_id.strip():
            raise ValueError("station_id must be a non-empty string")
        if not isinstance(self.bus, str) or not self.bus.strip():
            raise ValueError("bus must be a non-empty string")
        measured = _finite_real("measured_power_mw", self.measured_power_mw)
        available = _finite_real("available_power_mw", self.available_power_mw)
        minimum = _finite_real("minimum_power_mw", self.minimum_power_mw)
        if min(measured, available, minimum) < 0.0:
            raise ValueError("station power values must be nonnegative")
        if minimum > available:
            raise ValueError("minimum_power_mw cannot exceed available_power_mw")
        for name in ("ramp_down_mw_per_minute", "ramp_up_mw_per_minute"):
            value = getattr(self, name)
            if value is not None and _finite_real(name, value) < 0.0:
                raise ValueError(f"{name} must be nonnegative")
        statuses = (self.in_service, self.healthy, self.communication_ok, self.controllable)
        if any(not isinstance(value, bool) for value in statuses):
            raise ValueError("station status fields must be booleans")

    @property
    def controllable_measurements_valid(self) -> bool:
        return all((
            self.measured_power_validity.valid,
            self.available_power_validity.valid,
            self.status_validity.valid,
        ))


@dataclass(frozen=True)
class HardProtectionStatus:
    """最后一级硬保护的已归一化状态。"""

    active: bool
    validity: SignalValidity

    def __post_init__(self) -> None:
        if not isinstance(self.active, bool):
            raise ValueError("hard protection active must be a boolean")


@dataclass(frozen=True)
class GroupTelemetrySnapshot:
    """群控单周期使用的不可变一致快照。

    所有安全布尔量都是必填字段；只有显式 synthetic factory 才能统一假设成功。
    """

    snapshot_id: str
    time_minutes: float
    coherent: bool
    pcc_power_mw: float
    current_net_load_mw: float
    previous_net_load_mw: float
    maximum_load_mw: float
    pv_capacity_mw: float
    p_grid_max_mw: float
    pv_actual_mw: float
    reverse_flow_risk: ReverseFlowRiskEstimate
    pcc_validity: SignalValidity
    load_validity: SignalValidity
    pv_aggregate_validity: SignalValidity
    network_limits_validity: SignalValidity
    hard_protection: HardProtectionStatus
    stations: tuple[PVStationSnapshot, ...]
    measured_supervisor_curtailment_mw: float | None
    achieved_restoration_mw: float | None
    recovery_command_acknowledged: bool
    recovery_command_valid: bool
    controllable_pv_available: bool
    voltage_within_limits: bool
    line_capacity_within_limits: bool
    power_factor_within_limits: bool
    storage_soc_within_limits: bool
    recovery_rearm_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("snapshot_id must be a non-empty string")
        if not isinstance(self.coherent, bool):
            raise ValueError("coherent must be a boolean")
        for name in (
            "time_minutes", "pcc_power_mw", "current_net_load_mw",
            "previous_net_load_mw", "maximum_load_mw", "pv_capacity_mw",
            "p_grid_max_mw", "pv_actual_mw",
        ):
            _finite_real(name, getattr(self, name))
        booleans = (
            self.recovery_command_acknowledged,
            self.recovery_command_valid,
            self.controllable_pv_available,
            self.voltage_within_limits,
            self.line_capacity_within_limits,
            self.power_factor_within_limits,
            self.storage_soc_within_limits,
            self.recovery_rearm_requested,
        )
        if any(not isinstance(value, bool) for value in booleans):
            raise ValueError("snapshot safety and recovery fields must be booleans")
        station_ids = [station.station_id for station in self.stations]
        if len(set(station_ids)) != len(station_ids):
            raise ValueError("station ids must be unique within a snapshot")
        for name in ("measured_supervisor_curtailment_mw", "achieved_restoration_mw"):
            value = getattr(self, name)
            if value is not None and _finite_real(name, value) < 0.0:
                raise ValueError(f"{name} must be nonnegative")

    @property
    def aggregate_control_inputs_valid(self) -> bool:
        return self.coherent and all((
            self.pcc_validity.valid,
            self.load_validity.valid,
            self.pv_aggregate_validity.valid,
            self.network_limits_validity.valid,
            self.hard_protection.validity.valid,
        ))

    @classmethod
    def synthetic(
        cls,
        *,
        snapshot_id: str,
        time_minutes: float,
        pcc_power_mw: float,
        current_net_load_mw: float,
        previous_net_load_mw: float,
        maximum_load_mw: float,
        pv_capacity_mw: float,
        p_grid_max_mw: float,
        pv_actual_mw: float,
        reverse_flow_risk: ReverseFlowRiskEstimate,
        stations: tuple[PVStationSnapshot, ...],
        measured_supervisor_curtailment_mw: float | None,
        achieved_restoration_mw: float | None,
        hard_protection_active: bool,
        controllable_pv_available: bool,
        power_factor_within_limits: bool,
        storage_soc_within_limits: bool,
    ) -> "GroupTelemetrySnapshot":
        """显式构造确定性 SIL stub；生产 adapter 不应调用此方法。"""

        valid = SignalValidity.valid_signal()
        return cls(
            snapshot_id=snapshot_id,
            time_minutes=time_minutes,
            coherent=True,
            pcc_power_mw=pcc_power_mw,
            current_net_load_mw=current_net_load_mw,
            previous_net_load_mw=previous_net_load_mw,
            maximum_load_mw=maximum_load_mw,
            pv_capacity_mw=pv_capacity_mw,
            p_grid_max_mw=p_grid_max_mw,
            pv_actual_mw=pv_actual_mw,
            reverse_flow_risk=reverse_flow_risk,
            pcc_validity=valid,
            load_validity=valid,
            pv_aggregate_validity=valid,
            network_limits_validity=valid,
            hard_protection=HardProtectionStatus(hard_protection_active, valid),
            stations=stations,
            measured_supervisor_curtailment_mw=measured_supervisor_curtailment_mw,
            achieved_restoration_mw=achieved_restoration_mw,
            recovery_command_acknowledged=True,
            recovery_command_valid=True,
            controllable_pv_available=controllable_pv_available,
            voltage_within_limits=True,
            line_capacity_within_limits=True,
            power_factor_within_limits=power_factor_within_limits,
            storage_soc_within_limits=storage_soc_within_limits,
        )


class CapIntentAction(str, Enum):
    """有状态绝对限额仲裁使用的三态意图。"""

    NO_CHANGE = "no_change"
    SET_CAP = "set_cap"
    RELEASE_CAP = "release_cap"


@dataclass(frozen=True)
class StationCapIntent:
    owner_id: str
    station_id: str
    action: CapIntentAction
    absolute_cap_mw: float | None = None
    decision_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id must be a non-empty string")
        if not isinstance(self.station_id, str) or not self.station_id.strip():
            raise ValueError("station_id must be a non-empty string")
        if self.action is CapIntentAction.SET_CAP:
            if self.absolute_cap_mw is None:
                raise ValueError("SET_CAP requires absolute_cap_mw")
            if _finite_real("absolute_cap_mw", self.absolute_cap_mw) < 0.0:
                raise ValueError("absolute_cap_mw must be nonnegative")
        elif self.absolute_cap_mw is not None:
            raise ValueError("only SET_CAP may carry absolute_cap_mw")


@dataclass(frozen=True)
class ArbitratedStationCap:
    station_id: str
    device_available_mw: float
    effective_cap_mw: float
    owner_caps: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class PccSafetyConstraint:
    """An owner's absolute minimum PCC import requirement.

    The internal sign convention is always ``P_pcc > 0`` for import.  Owners
    submit constraints; they never mutate a device measurement directly.
    """

    owner_id: str
    minimum_import_mw: float
    decision_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id must be a non-empty string")
        if not isinstance(self.decision_id, str) or not self.decision_id.strip():
            raise ValueError("decision_id must be a non-empty string")
        if _finite_real("minimum_import_mw", self.minimum_import_mw) < 0.0:
            raise ValueError("minimum_import_mw must be nonnegative")


@dataclass(frozen=True)
class SafetyAllocationResult:
    """Traceable allocation of a PCC safety deficit across PV and wind/storage."""

    decision_id: str
    minimum_import_mw: float
    pcc_before_action_mw: float
    required_reduction_mw: float
    confirmed_pv_reduction_mw: float
    requested_wind_storage_reduction_mw: float
    allocated_wind_storage_reduction_mw: float
    unserved_reduction_mw: float
    wind_storage_target_mw: float | None
    command_accepted: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, str) or not self.decision_id.strip():
            raise ValueError("decision_id must be a non-empty string")
        for name in (
            "minimum_import_mw",
            "required_reduction_mw",
            "confirmed_pv_reduction_mw",
            "requested_wind_storage_reduction_mw",
            "allocated_wind_storage_reduction_mw",
            "unserved_reduction_mw",
        ):
            if _finite_real(name, getattr(self, name)) < 0.0:
                raise ValueError(f"{name} must be nonnegative")
        _finite_real("pcc_before_action_mw", self.pcc_before_action_mw)
        if self.wind_storage_target_mw is not None:
            if _finite_real(
                "wind_storage_target_mw", self.wind_storage_target_mw
            ) < 0.0:
                raise ValueError("wind_storage_target_mw must be nonnegative")
        if not isinstance(self.command_accepted, bool):
            raise ValueError("command_accepted must be a boolean")

    @property
    def fully_allocated(self) -> bool:
        return self.unserved_reduction_mw <= 1e-9


__all__ = [
    "ArbitratedStationCap",
    "CapIntentAction",
    "GroupTelemetrySnapshot",
    "HardProtectionStatus",
    "PVStationSnapshot",
    "ResolvedReverseFlowRisk",
    "ReverseFlowRiskEstimate",
    "PccSafetyConstraint",
    "SafetyAllocationResult",
    "SignalValidity",
    "StationActuationFeedback",
    "StationCapIntent",
]

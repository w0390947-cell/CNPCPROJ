"""山城单微网设备级确定性控制策略。

本模块实现《山城微电网控制策略逻辑》中可独立于优化器验证的控制底座：

* 无调度指令时执行风机尽发、储能分时充放电和 SVG 恒无功；
* 有调度指令时按明确优先级分解有功、无功目标；
* 基于实时设备状态计算并上送无符号可调裕度；
* 指令越限时保持原控制状态，不执行部分调节；
* 北向统一使用“发电/容性为正”的规范口径，仅在南向设备接口转换符号。

现有 MISOCP/ADMM 的 PCC ``P_grid`` 使用“受电为正”的另一套口径，因此这里故意
使用 ``controlled_active_target_mw``，避免把风储受控组合出力与 PCC 净交换功率混用。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from numbers import Real
from typing import Literal
from uuid import uuid4

from .modules.control.api import allocate_wind_storage_target, wind_storage_target_bounds
from .modules.control.contracts import StorageActiveCapability, WindActiveState

_POWER_TOLERANCE = 1e-9


def _finite_real(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)):
        raise ValueError(f"{name} must be a finite real number")
    return float(value)


def _nonnegative(name: str, value: float) -> float:
    number = _finite_real(name, value)
    if number < 0.0:
        raise ValueError(f"{name} must be nonnegative")
    return number


def _aware_utc(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def northbound_active_target_to_internal(value_mw: float) -> float:
    """北向有功目标：发电为正；内部受控组合出力同号。"""
    return _nonnegative("northbound active target", value_mw)


def northbound_reactive_target_to_internal(value_mvar: float) -> float:
    """北向无功目标：容性为正；内部无功同号。"""
    return _finite_real("northbound reactive target", value_mvar)


def internal_wind_active_to_southbound(value_mw: float) -> float:
    """风机南向有功：发电为正。"""
    return _nonnegative("internal wind active power", value_mw)


def internal_wind_reactive_to_southbound(value_mvar: float) -> float:
    """风机南向无功：容性为正、感性为负。"""
    return _finite_real("internal wind reactive power", value_mvar)


def internal_storage_active_to_southbound(value_mw: float) -> float:
    """储能南向有功：放电为正、充电为负。"""
    return _finite_real("internal storage active power", value_mw)


def internal_svg_reactive_to_southbound(value_mvar: float) -> float:
    """SVG 南向无功：容性为负、感性为正。"""
    return -_finite_real("internal SVG reactive power", value_mvar)


class ShanchengControlMode(str, Enum):
    LOCAL = "local"
    DISPATCH_TRACKING = "dispatch_tracking"
    # Backward-compatible enum alias. New code should use DISPATCH_TRACKING.
    DISPATCH = "dispatch_tracking"
    DEGRADED_HOLD = "degraded_hold"
    OUTPUT_BLOCK = "output_block"
    RECOVERY_INHIBIT = "recovery_inhibit"


class CommandDisposition(str, Enum):
    NONE = "none"
    TRACKING = "tracking"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    STALE = "stale"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    SUSPENDED = "suspended"
    SUSPENDED_EXPIRED = "suspended_expired"
    TIMED_OUT = "timed_out"
    EXITED_TO_LOCAL = "exited_to_local"


class ChannelAction(str, Enum):
    SET_TARGET = "set_target"
    ENTER_LOCAL = "enter_local"


@dataclass(frozen=True)
class SignalValidity:
    """Adapter-classified measurement quality; the domain does not guess staleness."""

    quality_good: bool = True
    fresh: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.quality_good, bool) or not isinstance(self.fresh, bool):
            raise ValueError("signal validity fields must be booleans")

    @property
    def valid(self) -> bool:
        return self.quality_good and self.fresh


@dataclass(frozen=True)
class ChannelActuationFeedback:
    """Field/adapter feedback. A calculated command is never treated as acknowledged."""

    execution_known: bool = True
    last_request_id: str | None = None
    last_acknowledged_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_known, bool):
            raise ValueError("execution_known must be a boolean")
        for name in ("last_request_id", "last_acknowledged_request_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be None or a non-empty string")


class ActiveMarginReporting(str, Enum):
    """上送有功下调裕度口径。

    PDF 正文只上送风机下调裕度，流程图却同时计算储能下调能力，因此保留两种
    可配置口径。控制器内部始终分别报告风机和储能分量。
    """

    WIND_ONLY = "wind_only"
    WIND_AND_STORAGE = "wind_and_storage"


@dataclass(frozen=True)
class DeviceStatus:
    remote_enabled: bool = True
    faulted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.remote_enabled, bool) or not isinstance(self.faulted, bool):
            raise ValueError("device status fields must be booleans")

    @property
    def available(self) -> bool:
        return self.remote_enabled and not self.faulted


@dataclass(frozen=True)
class StorageInterlocks:
    """12 组 BMS、12 组 PCS 和 PCS 总闭锁，共 25 个闭锁量。"""

    bms_lockouts: tuple[bool, ...] = field(default_factory=lambda: (False,) * 12)
    pcs_module_lockouts: tuple[bool, ...] = field(default_factory=lambda: (False,) * 12)
    pcs_total_lockout: bool = False

    def __post_init__(self) -> None:
        if len(self.bms_lockouts) != 12:
            raise ValueError("bms_lockouts must contain exactly 12 values")
        if len(self.pcs_module_lockouts) != 12:
            raise ValueError("pcs_module_lockouts must contain exactly 12 values")
        values = self.bms_lockouts + self.pcs_module_lockouts + (self.pcs_total_lockout,)
        if any(not isinstance(value, bool) for value in values):
            raise ValueError("storage lockout fields must be booleans")

    @property
    def any_locked(self) -> bool:
        return any(self.bms_lockouts) or any(self.pcs_module_lockouts) or self.pcs_total_lockout


@dataclass(frozen=True)
class WindTurbineTelemetry:
    name: str
    active_power_mw: float
    reactive_power_mvar: float
    available_active_power_mw: float
    rated_active_power_mw: float = 5.0
    status: DeviceStatus = field(default_factory=DeviceStatus)
    active_power_validity: SignalValidity = field(default_factory=SignalValidity)
    reactive_power_validity: SignalValidity = field(default_factory=SignalValidity)
    available_power_validity: SignalValidity = field(default_factory=SignalValidity)
    status_validity: SignalValidity = field(default_factory=SignalValidity)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("wind turbine name must be non-empty")
        active = _nonnegative("active_power_mw", self.active_power_mw)
        available = _nonnegative("available_active_power_mw", self.available_active_power_mw)
        rated = _nonnegative("rated_active_power_mw", self.rated_active_power_mw)
        _finite_real("reactive_power_mvar", self.reactive_power_mvar)
        if rated <= 0.0:
            raise ValueError("rated_active_power_mw must be positive")
        if active > rated + _POWER_TOLERANCE:
            raise ValueError("active_power_mw cannot exceed rated_active_power_mw")
        if available > rated + _POWER_TOLERANCE:
            raise ValueError("available_active_power_mw cannot exceed rated_active_power_mw")

    @property
    def active_measurements_valid(self) -> bool:
        return all((
            self.active_power_validity.valid,
            self.reactive_power_validity.valid,
            self.available_power_validity.valid,
            self.status_validity.valid,
        ))

    @property
    def reactive_measurements_valid(self) -> bool:
        return all((
            self.active_power_validity.valid,
            self.reactive_power_validity.valid,
            self.status_validity.valid,
        ))


@dataclass(frozen=True)
class StorageTelemetry:
    """储能遥测；有功正值为放电，负值为充电。"""

    active_power_mw: float
    energy_mwh: float
    energy_min_mwh: float
    energy_max_mwh: float
    charge_power_max_mw: float
    discharge_power_max_mw: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    status: DeviceStatus = field(default_factory=DeviceStatus)
    interlocks: StorageInterlocks = field(default_factory=StorageInterlocks)
    active_power_validity: SignalValidity = field(default_factory=SignalValidity)
    energy_validity: SignalValidity = field(default_factory=SignalValidity)
    status_validity: SignalValidity = field(default_factory=SignalValidity)
    interlocks_validity: SignalValidity = field(default_factory=SignalValidity)

    def __post_init__(self) -> None:
        active = _finite_real("storage.active_power_mw", self.active_power_mw)
        energy = _nonnegative("storage.energy_mwh", self.energy_mwh)
        energy_min = _nonnegative("storage.energy_min_mwh", self.energy_min_mwh)
        energy_max = _nonnegative("storage.energy_max_mwh", self.energy_max_mwh)
        charge_max = _nonnegative("storage.charge_power_max_mw", self.charge_power_max_mw)
        discharge_max = _nonnegative(
            "storage.discharge_power_max_mw", self.discharge_power_max_mw
        )
        eta_charge = _finite_real("storage.charge_efficiency", self.charge_efficiency)
        eta_discharge = _finite_real(
            "storage.discharge_efficiency", self.discharge_efficiency
        )
        if energy_max <= energy_min:
            raise ValueError("storage energy_max_mwh must exceed energy_min_mwh")
        if not energy_min - _POWER_TOLERANCE <= energy <= energy_max + _POWER_TOLERANCE:
            raise ValueError("storage energy must lie within its limits")
        if not 0.0 < eta_charge <= 1.0 or not 0.0 < eta_discharge <= 1.0:
            raise ValueError("storage efficiencies must be in (0, 1]")
        if active < -charge_max - _POWER_TOLERANCE or active > discharge_max + _POWER_TOLERANCE:
            raise ValueError("storage active power exceeds its charge/discharge rating")

    @property
    def available(self) -> bool:
        return self.status.available and not self.interlocks.any_locked

    @property
    def active_measurements_valid(self) -> bool:
        return all((
            self.active_power_validity.valid,
            self.energy_validity.valid,
            self.status_validity.valid,
            self.interlocks_validity.valid,
        ))


@dataclass(frozen=True)
class SvgTelemetry:
    """SVG 规范遥测；内部正值为容性、负值为感性。"""

    reactive_power_mvar: float
    status: DeviceStatus = field(default_factory=DeviceStatus)
    reactive_power_validity: SignalValidity = field(default_factory=SignalValidity)
    status_validity: SignalValidity = field(default_factory=SignalValidity)

    def __post_init__(self) -> None:
        _finite_real("svg.reactive_power_mvar", self.reactive_power_mvar)

    @property
    def reactive_measurements_valid(self) -> bool:
        return self.reactive_power_validity.valid and self.status_validity.valid


@dataclass(frozen=True)
class ShanchengTelemetry:
    snapshot_id: str
    observed_at_utc: datetime
    elapsed_minutes: float
    clock_hour: float
    step_minutes: float
    wind_turbines: tuple[WindTurbineTelemetry, ...]
    storage: StorageTelemetry
    svg: SvgTelemetry
    active_actuation: ChannelActuationFeedback = field(
        default_factory=ChannelActuationFeedback
    )
    reactive_actuation: ChannelActuationFeedback = field(
        default_factory=ChannelActuationFeedback
    )

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("snapshot_id must be non-empty")
        _aware_utc("observed_at_utc", self.observed_at_utc)
        elapsed = _nonnegative("elapsed_minutes", self.elapsed_minutes)
        hour = _finite_real("clock_hour", self.clock_hour)
        step = _finite_real("step_minutes", self.step_minutes)
        if not 0.0 <= hour < 24.0:
            raise ValueError("clock_hour must be in [0, 24)")
        if step <= 0.0:
            raise ValueError("step_minutes must be positive")
        if not self.wind_turbines:
            raise ValueError("at least one wind turbine is required")
        names = [item.name for item in self.wind_turbines]
        if len(set(names)) != len(names):
            raise ValueError("wind turbine names must be unique")
        del elapsed

    @property
    def active_measurements_valid(self) -> bool:
        return (
            all(item.active_measurements_valid for item in self.wind_turbines)
            and self.storage.active_measurements_valid
        )

    @property
    def reactive_measurements_valid(self) -> bool:
        return (
            all(item.reactive_measurements_valid for item in self.wind_turbines)
            and self.svg.reactive_measurements_valid
        )


@dataclass(frozen=True)
class ActiveRequest:
    sequence: int
    action: ChannelAction
    target_mw: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("active sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("active sequence must be nonnegative")
        if not isinstance(self.action, ChannelAction):
            raise ValueError("active action must be a ChannelAction")
        if self.action is ChannelAction.SET_TARGET:
            if self.target_mw is None:
                raise ValueError("SET_TARGET requires an active target")
            northbound_active_target_to_internal(self.target_mw)
        elif self.target_mw is not None:
            raise ValueError("ENTER_LOCAL must not contain an active target")

    @property
    def fingerprint(self) -> tuple[str, float | None]:
        return self.action.value, self.target_mw


@dataclass(frozen=True)
class ReactiveRequest:
    sequence: int
    action: ChannelAction
    target_mvar: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("reactive sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("reactive sequence must be nonnegative")
        if not isinstance(self.action, ChannelAction):
            raise ValueError("reactive action must be a ChannelAction")
        if self.action is ChannelAction.SET_TARGET:
            if self.target_mvar is None:
                raise ValueError("SET_TARGET requires a reactive target")
            northbound_reactive_target_to_internal(self.target_mvar)
        elif self.target_mvar is not None:
            raise ValueError("ENTER_LOCAL must not contain a reactive target")

    @property
    def fingerprint(self) -> tuple[str, float | None]:
        return self.action.value, self.target_mvar


@dataclass(frozen=True)
class CommandEnvelope:
    """Strict domain command envelope with per-channel replay metadata."""

    source_id: str
    source_epoch: str
    message_id: str
    issued_at_utc: datetime
    valid_until_utc: datetime
    active_request: ActiveRequest | None = None
    reactive_request: ReactiveRequest | None = None

    def __post_init__(self) -> None:
        for name in ("source_id", "source_epoch", "message_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        issued = _aware_utc("issued_at_utc", self.issued_at_utc)
        valid_until = _aware_utc("valid_until_utc", self.valid_until_utc)
        if valid_until < issued:
            raise ValueError("valid_until_utc must not precede issued_at_utc")
        if self.active_request is None and self.reactive_request is None:
            raise ValueError("a command envelope must contain a P or Q request")

    @property
    def command_id(self) -> str:
        return self.message_id

    @property
    def controlled_active_target_mw(self) -> float | None:
        request = self.active_request
        if request is None or request.action is ChannelAction.ENTER_LOCAL:
            return None
        return request.target_mw

    @property
    def capacitive_reactive_target_mvar(self) -> float | None:
        request = self.reactive_request
        if request is None or request.action is ChannelAction.ENTER_LOCAL:
            return None
        return request.target_mvar


# Retain the public project name while exposing the envelope contract explicitly.
ShanchengDispatchCommand = CommandEnvelope


class LegacyCommandFactory:
    """Explicit simulation compatibility that generates complete metadata."""

    def __init__(
        self,
        source_id: str = "legacy-simulation",
        source_epoch: str | None = None,
        validity_seconds: float = 3600.0,
    ) -> None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be non-empty")
        validity = _nonnegative("validity_seconds", validity_seconds)
        if validity <= 0.0:
            raise ValueError("validity_seconds must be positive")
        self.source_id = source_id
        self.source_epoch = source_epoch or str(uuid4())
        self.validity_seconds = validity
        self._active_sequence = 0
        self._reactive_sequence = 0

    def authorize(self, controller: "ShanchengController") -> None:
        controller.authorize_source_epoch(self.source_id, self.source_epoch)

    def make(
        self,
        *,
        now_utc: datetime,
        message_id: str | None = None,
        active_target_mw: float | None = None,
        reactive_target_mvar: float | None = None,
        enter_active_local: bool = False,
        enter_reactive_local: bool = False,
    ) -> CommandEnvelope:
        now = _aware_utc("now_utc", now_utc)
        if enter_active_local and active_target_mw is not None:
            raise ValueError("active target and ENTER_LOCAL are mutually exclusive")
        if enter_reactive_local and reactive_target_mvar is not None:
            raise ValueError("reactive target and ENTER_LOCAL are mutually exclusive")
        active = None
        reactive = None
        if active_target_mw is not None or enter_active_local:
            self._active_sequence += 1
            active = ActiveRequest(
                sequence=self._active_sequence,
                action=(
                    ChannelAction.ENTER_LOCAL
                    if enter_active_local else ChannelAction.SET_TARGET
                ),
                target_mw=active_target_mw,
            )
        if reactive_target_mvar is not None or enter_reactive_local:
            self._reactive_sequence += 1
            reactive = ReactiveRequest(
                sequence=self._reactive_sequence,
                action=(
                    ChannelAction.ENTER_LOCAL
                    if enter_reactive_local else ChannelAction.SET_TARGET
                ),
                target_mvar=reactive_target_mvar,
            )
        return CommandEnvelope(
            source_id=self.source_id,
            source_epoch=self.source_epoch,
            message_id=message_id or str(uuid4()),
            issued_at_utc=now,
            valid_until_utc=now + timedelta(seconds=self.validity_seconds),
            active_request=active,
            reactive_request=reactive,
        )


@dataclass(frozen=True)
class ShanchengControlConfig:
    local_charge_start_hour: float = 12.0
    local_charge_end_hour: float = 15.0
    local_discharge_start_hour: float = 20.0
    local_discharge_end_hour: float = 23.0
    local_charge_power_mw: float = 2.0
    local_svg_capacitive_mvar: float = 1.5
    wind_reactive_ratio: float = 0.30
    svg_dispatch_limit_mvar: float = 1.8
    active_deadband_mw: float = 0.0
    reactive_deadband_mvar: float = 0.0
    active_timeout_minutes: float = 15.0
    reactive_timeout_minutes: float = 15.0
    command_clock_skew_tolerance_seconds: float = 0.0
    local_discharge_accounts_for_efficiency: bool = True
    allow_storage_discharge_for_upward_dispatch: bool = False
    same_message_atomic: bool = True
    require_known_actuation_feedback: bool = True
    active_margin_reporting: ActiveMarginReporting = ActiveMarginReporting.WIND_ONLY

    def __post_init__(self) -> None:
        for name in (
            "local_charge_start_hour", "local_charge_end_hour",
            "local_discharge_start_hour", "local_discharge_end_hour",
        ):
            value = _finite_real(name, getattr(self, name))
            if not 0.0 <= value <= 24.0:
                raise ValueError(f"{name} must be in [0, 24]")
        if self.local_charge_start_hour >= self.local_charge_end_hour:
            raise ValueError("local charge window must not wrap midnight")
        if self.local_discharge_start_hour >= self.local_discharge_end_hour:
            raise ValueError("local discharge window must not wrap midnight")
        for name in (
            "local_charge_power_mw", "local_svg_capacitive_mvar",
            "wind_reactive_ratio", "svg_dispatch_limit_mvar",
            "active_deadband_mw", "reactive_deadband_mvar",
            "active_timeout_minutes", "reactive_timeout_minutes",
            "command_clock_skew_tolerance_seconds",
        ):
            _nonnegative(name, getattr(self, name))
        if self.active_timeout_minutes <= 0.0 or self.reactive_timeout_minutes <= 0.0:
            raise ValueError("command timeouts must be positive")
        if self.local_svg_capacitive_mvar > self.svg_dispatch_limit_mvar:
            raise ValueError("local SVG setpoint cannot exceed its dispatch limit")
        boolean_values = {
            "local_discharge_accounts_for_efficiency": (
                self.local_discharge_accounts_for_efficiency
            ),
            "allow_storage_discharge_for_upward_dispatch": (
                self.allow_storage_discharge_for_upward_dispatch
            ),
            "same_message_atomic": self.same_message_atomic,
            "require_known_actuation_feedback": (
                self.require_known_actuation_feedback
            ),
        }
        invalid = [name for name, value in boolean_values.items() if not isinstance(value, bool)]
        if invalid:
            raise ValueError(f"boolean configuration required: {', '.join(invalid)}")


@dataclass(frozen=True)
class WindSetpoint:
    name: str
    active_power_mw: float | None
    reactive_power_mvar: float | None


@dataclass(frozen=True)
class ShanchengSetpoints:
    wind: tuple[WindSetpoint, ...]
    storage_active_power_mw: float | None
    storage_reactive_power_mvar: float | None
    svg_capacitive_reactive_power_mvar: float | None

    @property
    def svg_southbound_reactive_power_mvar(self) -> float | None:
        """SVG 南向接口采用容性为负。"""
        if self.svg_capacitive_reactive_power_mvar is None:
            return None
        return internal_svg_reactive_to_southbound(
            self.svg_capacitive_reactive_power_mvar
        )


@dataclass(frozen=True)
class ShanchengCapabilityReport:
    """上送值均为无符号能力幅值，内部同时保留分设备分量。"""

    active_valid: bool
    reactive_valid: bool
    joint_pq_valid: bool
    wind_active_down_mw: float
    wind_active_up_mw: float
    storage_charge_capability_mw: float
    storage_transition_down_mw: float
    storage_discharge_capability_mw: float
    dispatch_active_down_mw: float
    dispatch_active_up_mw: float
    reported_active_down_mw: float
    wind_reactive_up_mvar: float
    wind_reactive_down_mvar: float
    svg_reactive_up_mvar: float
    svg_reactive_down_mvar: float
    reactive_up_mvar: float
    reactive_down_mvar: float
    minimum_active_target_mw: float | None
    maximum_active_target_mw: float | None
    minimum_reactive_target_mvar: float | None
    maximum_reactive_target_mvar: float | None
    invalid_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShanchengControlAlarm:
    channel: str
    code: str
    message: str
    requested_value: float | None = None
    minimum_value: float | None = None
    maximum_value: float | None = None


@dataclass(frozen=True)
class ShanchengControlDecision:
    active_mode: ShanchengControlMode
    reactive_mode: ShanchengControlMode
    active_disposition: CommandDisposition
    reactive_disposition: CommandDisposition
    active_target_mw: float | None
    reactive_target_mvar: float | None
    active_suspended_target_mw: float | None
    reactive_suspended_target_mvar: float | None
    setpoints: ShanchengSetpoints
    capabilities: ShanchengCapabilityReport
    alarms: tuple[ShanchengControlAlarm, ...] = ()
    command_id: str | None = None


def _storage_capabilities(
    telemetry: ShanchengTelemetry,
) -> tuple[float, float, float]:
    storage = telemetry.storage
    if not storage.available:
        return 0.0, 0.0, 0.0
    dt_hours = telemetry.step_minutes / 60.0
    charge = min(
        storage.charge_power_max_mw,
        max(0.0, storage.energy_max_mwh - storage.energy_mwh)
        / (storage.charge_efficiency * dt_hours),
    )
    discharge = min(
        storage.discharge_power_max_mw,
        max(0.0, storage.energy_mwh - storage.energy_min_mwh)
        * storage.discharge_efficiency
        / dt_hours,
    )
    transition_down = max(0.0, storage.active_power_mw + charge)
    return charge, discharge, transition_down


def calculate_shancheng_capabilities(
    telemetry: ShanchengTelemetry,
    config: ShanchengControlConfig | None = None,
) -> ShanchengCapabilityReport:
    cfg = config or ShanchengControlConfig()
    active_valid = telemetry.active_measurements_valid
    reactive_valid = telemetry.reactive_measurements_valid
    invalid_reasons: list[str] = []
    for item in telemetry.wind_turbines:
        if not item.active_measurements_valid:
            invalid_reasons.append(f"WIND_ACTIVE_INVALID:{item.name}")
        if not item.reactive_measurements_valid:
            invalid_reasons.append(f"WIND_REACTIVE_INVALID:{item.name}")
    if not telemetry.storage.active_measurements_valid:
        invalid_reasons.append("STORAGE_ACTIVE_INVALID")
    if not telemetry.svg.reactive_measurements_valid:
        invalid_reasons.append("SVG_REACTIVE_INVALID")
    healthy_wind = [item for item in telemetry.wind_turbines if item.status.available]
    wind_p_down = sum(item.active_power_mw for item in healthy_wind)
    wind_p_up = sum(
        max(0.0, item.available_active_power_mw - item.active_power_mw)
        for item in healthy_wind
    )
    wind_q_up = sum(
        max(0.0, cfg.wind_reactive_ratio * item.active_power_mw - item.reactive_power_mvar)
        for item in healthy_wind
    )
    wind_q_down = sum(
        max(0.0, cfg.wind_reactive_ratio * item.active_power_mw + item.reactive_power_mvar)
        for item in healthy_wind
    )
    svg_q = telemetry.svg.reactive_power_mvar
    if telemetry.svg.status.available:
        svg_q_up = max(0.0, cfg.svg_dispatch_limit_mvar - svg_q)
        svg_q_down = max(0.0, cfg.svg_dispatch_limit_mvar + svg_q)
    else:
        svg_q_up = svg_q_down = 0.0

    charge, discharge, transition_down = _storage_capabilities(telemetry)
    reported_down = (
        wind_p_down
        if cfg.active_margin_reporting is ActiveMarginReporting.WIND_ONLY
        else wind_p_down + transition_down
    )
    current_q = sum(item.reactive_power_mvar for item in telemetry.wind_turbines) + svg_q
    if not active_valid:
        wind_p_down = wind_p_up = 0.0
        charge = discharge = transition_down = 0.0
        dispatch_down = dispatch_up = reported_down = 0.0
        minimum_active = maximum_active = None
    else:
        wind_states, storage_capability = _active_dispatch_inputs(telemetry, cfg)
        minimum_active, maximum_active = wind_storage_target_bounds(wind_states, storage_capability)
        baseline = sum(item.active_power_mw for item in telemetry.wind_turbines)
        baseline += 0.0 if telemetry.storage.available else telemetry.storage.active_power_mw
        dispatch_down = max(0.0, baseline - minimum_active)
        dispatch_up = max(0.0, maximum_active - baseline)
    if not reactive_valid:
        wind_q_up = wind_q_down = 0.0
        svg_q_up = svg_q_down = 0.0
        minimum_reactive = maximum_reactive = None
    else:
        minimum_reactive = current_q - wind_q_down - svg_q_down
        maximum_reactive = current_q + wind_q_up + svg_q_up
    return ShanchengCapabilityReport(
        active_valid=active_valid,
        reactive_valid=reactive_valid,
        joint_pq_valid=active_valid and reactive_valid,
        wind_active_down_mw=wind_p_down,
        wind_active_up_mw=wind_p_up,
        storage_charge_capability_mw=charge,
        storage_transition_down_mw=transition_down,
        storage_discharge_capability_mw=discharge,
        dispatch_active_down_mw=dispatch_down,
        dispatch_active_up_mw=dispatch_up,
        reported_active_down_mw=reported_down,
        wind_reactive_up_mvar=wind_q_up,
        wind_reactive_down_mvar=wind_q_down,
        svg_reactive_up_mvar=svg_q_up,
        svg_reactive_down_mvar=svg_q_down,
        reactive_up_mvar=wind_q_up + svg_q_up,
        reactive_down_mvar=wind_q_down + svg_q_down,
        minimum_active_target_mw=minimum_active,
        maximum_active_target_mw=maximum_active,
        minimum_reactive_target_mvar=minimum_reactive,
        maximum_reactive_target_mvar=maximum_reactive,
        invalid_reasons=tuple(dict.fromkeys(invalid_reasons)),
    )


def _proportional_allocation(request: float, capacities: list[float]) -> list[float]:
    allocation = [0.0 for _ in capacities]
    remaining = max(0.0, request)
    active = {index for index, capacity in enumerate(capacities) if capacity > _POWER_TOLERANCE}
    while remaining > _POWER_TOLERANCE and active:
        total_capacity = sum(capacities[index] - allocation[index] for index in active)
        if total_capacity <= _POWER_TOLERANCE:
            break
        consumed = 0.0
        for index in tuple(active):
            headroom = capacities[index] - allocation[index]
            share = min(headroom, remaining * headroom / total_capacity)
            allocation[index] += share
            consumed += share
            if headroom - share <= _POWER_TOLERANCE:
                active.discard(index)
        if consumed <= _POWER_TOLERANCE:
            break
        remaining -= consumed
    return allocation


def _in_window(hour: float, start: float, end: float) -> bool:
    return start <= hour < end


def local_shancheng_setpoints(
    telemetry: ShanchengTelemetry,
    config: ShanchengControlConfig | None = None,
) -> ShanchengSetpoints:
    cfg = config or ShanchengControlConfig()
    wind = tuple(
        WindSetpoint(
            name=item.name,
            active_power_mw=(
                item.available_active_power_mw
                if item.status.available and item.active_measurements_valid else None
            ),
            reactive_power_mvar=None,
        )
        for item in telemetry.wind_turbines
    )
    storage = telemetry.storage
    storage_p: float | None
    if not storage.available or not storage.active_measurements_valid:
        storage_p = None
    elif _in_window(
        telemetry.clock_hour, cfg.local_charge_start_hour, cfg.local_charge_end_hour
    ):
        charge, _, _ = _storage_capabilities(telemetry)
        storage_p = -min(cfg.local_charge_power_mw, charge)
    elif _in_window(
        telemetry.clock_hour,
        cfg.local_discharge_start_hour,
        cfg.local_discharge_end_hour,
    ):
        remaining_hours = cfg.local_discharge_end_hour - telemetry.clock_hour
        dischargeable_output_mwh = max(
            0.0, storage.energy_mwh - storage.energy_min_mwh
        )
        if cfg.local_discharge_accounts_for_efficiency:
            dischargeable_output_mwh *= storage.discharge_efficiency
        _, interval_discharge, _ = _storage_capabilities(telemetry)
        storage_p = min(
            storage.discharge_power_max_mw,
            dischargeable_output_mwh / remaining_hours,
            interval_discharge,
        )
    else:
        storage_p = 0.0

    svg_q = (
        cfg.local_svg_capacitive_mvar
        if (
            telemetry.svg.status.available
            and telemetry.svg.reactive_measurements_valid
        ) else None
    )
    storage_q_available = (
        storage.available
        and storage.status_validity.valid
        and storage.interlocks_validity.valid
    )
    return ShanchengSetpoints(
        wind=wind,
        storage_active_power_mw=storage_p,
        storage_reactive_power_mvar=0.0 if storage_q_available else None,
        svg_capacitive_reactive_power_mvar=svg_q,
    )


def _active_dispatch_inputs(
    telemetry: ShanchengTelemetry,
    config: ShanchengControlConfig,
) -> tuple[tuple[WindActiveState, ...], StorageActiveCapability]:
    """Translate validated legacy telemetry without changing measured facts."""
    charge, discharge, _ = _storage_capabilities(telemetry)
    return (
        tuple(
            WindActiveState(
                item.name,
                item.active_power_mw,
                item.available_active_power_mw,
                item.status.available,
            )
            for item in telemetry.wind_turbines
        ),
        StorageActiveCapability(
            telemetry.storage.active_power_mw,
            telemetry.storage.available,
            charge,
            discharge if config.allow_storage_discharge_for_upward_dispatch else 0.0,
        ),
    )


def _active_decomposition(
    telemetry: ShanchengTelemetry,
    target_mw: float,
    config: ShanchengControlConfig,
    capabilities: ShanchengCapabilityReport,
) -> tuple[tuple[float | None, ...], float | None] | None:
    if not capabilities.active_valid:
        return None
    wind_states, storage_capability = _active_dispatch_inputs(telemetry, config)
    proposal = allocate_wind_storage_target(
        wind_states,
        storage_capability,
        target_mw,
        deadband_mw=config.active_deadband_mw,
    )
    if proposal is None:
        return None
    targets = {item.resource_id: item.target_mw for item in proposal.wind}
    return (
        tuple(targets[item.name] for item in telemetry.wind_turbines),
        proposal.storage_target_mw,
    )


def _reactive_decomposition(
    telemetry: ShanchengTelemetry,
    target_mvar: float,
    config: ShanchengControlConfig,
    effective_wind_active_mw: tuple[float, ...],
) -> tuple[tuple[float | None, ...], float | None] | None:
    if len(effective_wind_active_mw) != len(telemetry.wind_turbines):
        raise ValueError("effective wind active vector has the wrong length")

    wind_targets: list[float | None] = []
    wind_lower: list[float] = []
    wind_upper: list[float] = []
    fixed_total = 0.0
    for item, effective_p in zip(
        telemetry.wind_turbines, effective_wind_active_mw, strict=True
    ):
        q_limit = config.wind_reactive_ratio * max(0.0, effective_p)
        if item.status.available:
            wind_lower.append(-q_limit)
            wind_upper.append(q_limit)
            wind_targets.append(min(q_limit, max(-q_limit, item.reactive_power_mvar)))
        else:
            wind_lower.append(0.0)
            wind_upper.append(0.0)
            wind_targets.append(None)
            fixed_total += item.reactive_power_mvar

    svg_available = telemetry.svg.status.available
    if svg_available:
        svg_lower = -config.svg_dispatch_limit_mvar
        svg_upper = config.svg_dispatch_limit_mvar
        svg_target: float | None = min(
            svg_upper, max(svg_lower, telemetry.svg.reactive_power_mvar)
        )
    else:
        svg_lower = svg_upper = 0.0
        svg_target = None
        fixed_total += telemetry.svg.reactive_power_mvar

    minimum = fixed_total + sum(wind_lower) + svg_lower
    maximum = fixed_total + sum(wind_upper) + svg_upper
    if target_mvar < minimum - _POWER_TOLERANCE:
        return None
    if target_mvar > maximum + _POWER_TOLERANCE:
        return None

    base_total = fixed_total + sum(
        value for value in wind_targets if value is not None
    )
    if svg_target is not None:
        base_total += svg_target
    delta = target_mvar - base_total
    if delta > config.reactive_deadband_mvar:
        svg_increase = 0.0
        if svg_target is not None:
            svg_increase = min(delta, svg_upper - svg_target)
            svg_target += svg_increase
        remaining = delta - svg_increase
        capacities = [
            upper - value if value is not None else 0.0
            for upper, value in zip(wind_upper, wind_targets, strict=True)
        ]
        increases = _proportional_allocation(remaining, capacities)
        for index, increase in enumerate(increases):
            if wind_targets[index] is not None:
                wind_targets[index] += increase
    elif delta < -config.reactive_deadband_mvar:
        request = -delta
        capacities = [
            value - lower if value is not None else 0.0
            for lower, value in zip(wind_lower, wind_targets, strict=True)
        ]
        reductions = _proportional_allocation(request, capacities)
        for index, reduction in enumerate(reductions):
            if wind_targets[index] is not None:
                wind_targets[index] -= reduction
        remaining = request - sum(reductions)
        if svg_target is not None:
            svg_target -= min(remaining, svg_target - svg_lower)

    return tuple(wind_targets), svg_target


@dataclass
class _ChannelRuntime:
    state: ShanchengControlMode = ShanchengControlMode.LOCAL
    target: float | None = None
    suspended_target: float | None = None
    accepted_at: float | None = None
    deadline: float | None = None


@dataclass(frozen=True)
class _PlanResult:
    setpoints: ShanchengSetpoints
    active_feasible: bool
    reactive_feasible: bool
    joint_feasible: bool

    @property
    def feasible(self) -> bool:
        return self.active_feasible and self.reactive_feasible and self.joint_feasible


def _channel_emits(state: ShanchengControlMode) -> bool:
    return state in (
        ShanchengControlMode.LOCAL,
        ShanchengControlMode.DISPATCH_TRACKING,
    )


def _build_control_plan(
    telemetry: ShanchengTelemetry,
    config: ShanchengControlConfig,
    capabilities: ShanchengCapabilityReport,
    active: _ChannelRuntime,
    reactive: _ChannelRuntime,
) -> _PlanResult:
    local = local_shancheng_setpoints(telemetry, config)
    active_ok = True
    reactive_ok = True

    if active.state is ShanchengControlMode.LOCAL:
        wind_p = tuple(item.active_power_mw for item in local.wind)
        storage_p = local.storage_active_power_mw
        active_ok = capabilities.active_valid
    elif active.state is ShanchengControlMode.DISPATCH_TRACKING:
        proposal = (
            _active_decomposition(telemetry, active.target, config, capabilities)
            if capabilities.active_valid and active.target is not None else None
        )
        active_ok = proposal is not None
        if proposal is None:
            wind_p = tuple(None for _ in telemetry.wind_turbines)
            storage_p = None
        else:
            wind_p, storage_p = proposal
    else:
        wind_p = tuple(None for _ in telemetry.wind_turbines)
        storage_p = None

    effective_p = tuple(
        min(item.active_power_mw, command_p)
        if command_p is not None else item.active_power_mw
        for item, command_p in zip(telemetry.wind_turbines, wind_p, strict=True)
    )

    if reactive.state is ShanchengControlMode.LOCAL:
        wind_q = tuple(item.reactive_power_mvar for item in local.wind)
        storage_q = local.storage_reactive_power_mvar
        svg_q = local.svg_capacitive_reactive_power_mvar
        reactive_ok = capabilities.reactive_valid
    elif reactive.state is ShanchengControlMode.DISPATCH_TRACKING:
        proposal = (
            _reactive_decomposition(
                telemetry, reactive.target, config, effective_p
            )
            if capabilities.reactive_valid and reactive.target is not None else None
        )
        reactive_ok = proposal is not None
        if proposal is None:
            wind_q = tuple(None for _ in telemetry.wind_turbines)
            svg_q = None
        else:
            wind_q, svg_q = proposal
        storage_q = (
            0.0
            if (
                telemetry.storage.available
                and telemetry.storage.status_validity.valid
                and telemetry.storage.interlocks_validity.valid
            ) else None
        )
    else:
        wind_q = tuple(None for _ in telemetry.wind_turbines)
        storage_q = None
        svg_q = None

    setpoints = ShanchengSetpoints(
        wind=tuple(
            WindSetpoint(item.name, p_value, q_value)
            for item, p_value, q_value in zip(
                telemetry.wind_turbines, wind_p, wind_q, strict=True
            )
        ),
        storage_active_power_mw=storage_p,
        storage_reactive_power_mvar=storage_q,
        svg_capacitive_reactive_power_mvar=svg_q,
    )

    joint_needed = _channel_emits(active.state) or _channel_emits(reactive.state)
    joint_ok = True
    if joint_needed:
        for item, p_effective, q_command in zip(
            telemetry.wind_turbines, effective_p, wind_q, strict=True
        ):
            q_effective = (
                item.reactive_power_mvar if q_command is None else q_command
            )
            q_limit = config.wind_reactive_ratio * p_effective
            if abs(q_effective) > q_limit + _POWER_TOLERANCE:
                joint_ok = False
                break
    return _PlanResult(setpoints, active_ok, reactive_ok, joint_ok)


class ShanchengController:
    """Transactional P/Q controller with replay protection and fail-silent output."""

    def __init__(self, config: ShanchengControlConfig | None = None) -> None:
        self.config = config or ShanchengControlConfig()
        self._active = _ChannelRuntime()
        self._reactive = _ChannelRuntime()
        self._last_step_time: float | None = None
        self._authorized_epochs: dict[str, str] = {}
        self._last_seen: dict[
            tuple[str, str, Literal["p", "q"]],
            tuple[int, tuple[str, float | None]],
        ] = {}
        self._last_accepted: dict[
            tuple[str, str, Literal["p", "q"]], int
        ] = {}

    @property
    def active_mode(self) -> ShanchengControlMode:
        return self._active.state

    @property
    def reactive_mode(self) -> ShanchengControlMode:
        return self._reactive.state

    def authorize_source_epoch(self, source_id: str, source_epoch: str) -> None:
        """Authorize a session established by an adapter or test harness."""
        for name, value in (("source_id", source_id), ("source_epoch", source_epoch)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        old_epoch = self._authorized_epochs.get(source_id)
        self._authorized_epochs[source_id] = source_epoch
        if old_epoch is not None and old_epoch != source_epoch:
            self._last_seen = {
                key: value for key, value in self._last_seen.items()
                if key[0] != source_id
            }
            self._last_accepted = {
                key: value for key, value in self._last_accepted.items()
                if key[0] != source_id
            }

    @staticmethod
    def _alarm(
        channel: Literal["p", "q"],
        code: str,
        message: str,
        requested: float | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> ShanchengControlAlarm:
        return ShanchengControlAlarm(
            channel=channel,
            code=code,
            message=message,
            requested_value=requested,
            minimum_value=minimum,
            maximum_value=maximum,
        )

    @staticmethod
    def _suspend(runtime: _ChannelRuntime, state: ShanchengControlMode) -> None:
        if runtime.target is not None:
            runtime.suspended_target = runtime.target
        runtime.target = None
        runtime.state = state

    @staticmethod
    def _enter_local(runtime: _ChannelRuntime) -> None:
        runtime.state = ShanchengControlMode.LOCAL
        runtime.target = None
        runtime.suspended_target = None
        runtime.accepted_at = None
        runtime.deadline = None

    def _channel_health(
        self,
        telemetry: ShanchengTelemetry,
        capabilities: ShanchengCapabilityReport,
        channel: Literal["p", "q"],
    ) -> tuple[bool, str | None]:
        if channel == "p":
            if not capabilities.active_valid:
                return False, "P_TELEMETRY_INVALID"
            feedback = telemetry.active_actuation
        else:
            if not capabilities.reactive_valid:
                return False, "Q_TELEMETRY_INVALID"
            feedback = telemetry.reactive_actuation
        if self.config.require_known_actuation_feedback and not feedback.execution_known:
            return False, f"{channel.upper()}_EXECUTION_UNKNOWN"
        return True, None

    def _apply_health_state(
        self,
        runtime: _ChannelRuntime,
        channel: Literal["p", "q"],
        healthy: bool,
        reason: str | None,
        alarms: list[ShanchengControlAlarm],
    ) -> bool:
        was_output_block = runtime.state is ShanchengControlMode.OUTPUT_BLOCK
        if not healthy:
            if not was_output_block:
                self._suspend(runtime, ShanchengControlMode.OUTPUT_BLOCK)
                alarms.append(self._alarm(
                    channel,
                    reason or f"{channel.upper()}_OUTPUT_BLOCKED",
                    "Channel output blocked because required information is uncertain",
                    requested=runtime.suspended_target,
                ))
            return False
        if was_output_block:
            runtime.state = ShanchengControlMode.RECOVERY_INHIBIT
        return True

    def _mark_capability_loss(
        self,
        runtime: _ChannelRuntime,
        channel: Literal["p", "q"],
        capabilities: ShanchengCapabilityReport,
        alarms: list[ShanchengControlAlarm],
    ) -> None:
        if not _channel_emits(runtime.state):
            return
        was_local = runtime.state is ShanchengControlMode.LOCAL
        requested = runtime.target
        self._suspend(runtime, ShanchengControlMode.DEGRADED_HOLD)
        minimum = (
            capabilities.minimum_active_target_mw
            if channel == "p" else capabilities.minimum_reactive_target_mvar
        )
        maximum = (
            capabilities.maximum_active_target_mw
            if channel == "p" else capabilities.maximum_reactive_target_mvar
        )
        alarms.append(
            self._alarm(
                channel,
                f"{channel.upper()}_LOCAL_PLAN_INFEASIBLE"
                if was_local
                else f"{channel.upper()}_TARGET_BECAME_INFEASIBLE",
                "Control plan became infeasible; new writes are suspended",
                requested,
                minimum,
                maximum,
            )
        )

    def _revalidate_current_plan(
        self,
        telemetry: ShanchengTelemetry,
        capabilities: ShanchengCapabilityReport,
        alarms: list[ShanchengControlAlarm],
    ) -> None:
        plan = _build_control_plan(
            telemetry, self.config, capabilities, self._active, self._reactive
        )
        if not plan.active_feasible:
            self._mark_capability_loss(self._active, "p", capabilities, alarms)
        if not plan.reactive_feasible:
            if (
                self._active.state is ShanchengControlMode.DISPATCH_TRACKING
                and self._reactive.state is ShanchengControlMode.DISPATCH_TRACKING
            ):
                self._mark_capability_loss(self._active, "p", capabilities, alarms)
            self._mark_capability_loss(self._reactive, "q", capabilities, alarms)
        if not plan.joint_feasible:
            # LOCAL is a writer too. No emitting mode may bypass the same
            # transition envelope; None never asserts a safe field state.
            self._mark_capability_loss(self._active, "p", capabilities, alarms)
            self._mark_capability_loss(self._reactive, "q", capabilities, alarms)

    def _process_timeout(
        self,
        runtime: _ChannelRuntime,
        channel: Literal["p", "q"],
        now: float,
        healthy: bool,
        alarms: list[ShanchengControlAlarm],
    ) -> CommandDisposition:
        if runtime.deadline is None or now < runtime.deadline:
            return CommandDisposition.NONE
        if runtime.state is ShanchengControlMode.DISPATCH_TRACKING and healthy:
            self._enter_local(runtime)
            alarms.append(self._alarm(
                channel,
                f"{channel.upper()}_COMMAND_TIMEOUT",
                "Dispatch command timed out and the healthy channel entered local mode",
            ))
            return CommandDisposition.TIMED_OUT
        if runtime.state in (
            ShanchengControlMode.DEGRADED_HOLD,
            ShanchengControlMode.OUTPUT_BLOCK,
            ShanchengControlMode.RECOVERY_INHIBIT,
        ):
            runtime.target = None
            runtime.suspended_target = None
            runtime.accepted_at = None
            runtime.deadline = None
            alarms.append(self._alarm(
                channel,
                f"{channel.upper()}_SUSPENDED_TARGET_EXPIRED",
                "Suspended target expired without releasing the channel to local output",
            ))
            return CommandDisposition.SUSPENDED_EXPIRED
        return CommandDisposition.NONE

    def _attempt_recovery_transition(
        self,
        telemetry: ShanchengTelemetry,
        capabilities: ShanchengCapabilityReport,
        channel: Literal["p", "q"],
        healthy: bool,
    ) -> None:
        runtime = self._active if channel == "p" else self._reactive
        if runtime.state is not ShanchengControlMode.DEGRADED_HOLD or not healthy:
            return
        candidate_active = replace(self._active)
        candidate_reactive = replace(self._reactive)
        candidate = candidate_active if channel == "p" else candidate_reactive
        candidate.state = (
            ShanchengControlMode.LOCAL
            if runtime.suspended_target is None
            else ShanchengControlMode.DISPATCH_TRACKING
        )
        candidate.target = runtime.suspended_target
        candidate.suspended_target = None
        plan = _build_control_plan(
            telemetry,
            self.config,
            capabilities,
            candidate_active,
            candidate_reactive,
        )
        if plan.feasible:
            runtime.state = ShanchengControlMode.RECOVERY_INHIBIT

    def _replay_status(
        self,
        command: CommandEnvelope,
        channel: Literal["p", "q"],
        request: ActiveRequest | ReactiveRequest,
    ) -> Literal["new", "duplicate", "stale", "collision", "unauthorized"]:
        if self._authorized_epochs.get(command.source_id) != command.source_epoch:
            return "unauthorized"
        key = (command.source_id, command.source_epoch, channel)
        seen = self._last_seen.get(key)
        if seen is not None:
            seen_sequence, seen_fingerprint = seen
            if request.sequence < seen_sequence:
                return "stale"
            if request.sequence == seen_sequence:
                return (
                    "duplicate"
                    if request.fingerprint == seen_fingerprint else "collision"
                )
        self._last_seen[key] = (request.sequence, request.fingerprint)
        return "new"

    def _freshness_status(
        self,
        command: CommandEnvelope,
        observed_at: datetime,
    ) -> Literal["fresh", "expired", "future"]:
        observed = _aware_utc("observed_at_utc", observed_at)
        skew = timedelta(seconds=self.config.command_clock_skew_tolerance_seconds)
        if command.issued_at_utc > observed + skew:
            return "future"
        if command.valid_until_utc < observed - skew:
            return "expired"
        return "fresh"

    @staticmethod
    def _candidate_runtime(
        runtime: _ChannelRuntime,
        request: ActiveRequest | ReactiveRequest,
        now: float,
        timeout: float,
    ) -> _ChannelRuntime:
        candidate = replace(runtime)
        if request.action is ChannelAction.ENTER_LOCAL:
            ShanchengController._enter_local(candidate)
        else:
            candidate.state = ShanchengControlMode.DISPATCH_TRACKING
            candidate.target = (
                request.target_mw
                if isinstance(request, ActiveRequest) else request.target_mvar
            )
            candidate.suspended_target = None
            candidate.accepted_at = now
            candidate.deadline = now + timeout
        return candidate

    def _record_accepted(
        self,
        command: CommandEnvelope,
        channel: Literal["p", "q"],
        request: ActiveRequest | ReactiveRequest,
    ) -> None:
        key = (command.source_id, command.source_epoch, channel)
        self._last_accepted[key] = request.sequence

    def _protocol_alarm(
        self,
        channel: Literal["p", "q"],
        status: str,
    ) -> ShanchengControlAlarm:
        codes = {
            "duplicate": "COMMAND_DUPLICATE",
            "stale": "COMMAND_STALE",
            "collision": "SEQUENCE_COLLISION",
            "unauthorized": "SOURCE_EPOCH_NOT_AUTHORIZED",
            "expired": "COMMAND_EXPIRED",
            "future": "COMMAND_NOT_YET_VALID",
            "atomic": "ATOMIC_COMMAND_REJECTED",
        }
        return self._alarm(
            channel,
            codes[status],
            f"Command was not committed because protocol status is {status}",
        )

    @staticmethod
    def _status_disposition(status: str) -> CommandDisposition:
        if status == "duplicate":
            return CommandDisposition.DUPLICATE
        if status == "stale":
            return CommandDisposition.STALE
        if status == "expired":
            return CommandDisposition.EXPIRED
        return CommandDisposition.REJECTED

    def _candidate_alarm(
        self,
        channel: Literal["p", "q"],
        request: ActiveRequest | ReactiveRequest,
        plan: _PlanResult,
        capabilities: ShanchengCapabilityReport,
        conflicts_with_retained: bool,
    ) -> ShanchengControlAlarm:
        target = (
            request.target_mw
            if isinstance(request, ActiveRequest) else request.target_mvar
        )
        if conflicts_with_retained:
            other_letter = "Q" if channel == "p" else "P"
            return self._alarm(
                channel,
                f"{channel.upper()}_CONFLICTS_WITH_RETAINED_{other_letter}",
                "New request would invalidate the retained channel target",
                target,
            )
        own_feasible = plan.active_feasible if channel == "p" else plan.reactive_feasible
        if not own_feasible:
            minimum = (
                capabilities.minimum_active_target_mw
                if channel == "p" else capabilities.minimum_reactive_target_mvar
            )
            maximum = (
                capabilities.maximum_active_target_mw
                if channel == "p" else capabilities.maximum_reactive_target_mvar
            )
            return self._alarm(
                channel,
                f"{channel.upper()}_TARGET_OUT_OF_RANGE",
                "Target is outside current adjustable capability; no state was changed",
                target,
                minimum,
                maximum,
            )
        return self._alarm(
            channel,
            f"{channel.upper()}_JOINT_ENVELOPE_VIOLATION",
            "New request conflicts with the retained channel or wind P-Q envelope",
            target,
        )

    def _commit_single_request(
        self,
        telemetry: ShanchengTelemetry,
        capabilities: ShanchengCapabilityReport,
        command: CommandEnvelope,
        channel: Literal["p", "q"],
        request: ActiveRequest | ReactiveRequest,
        healthy: bool,
        now: float,
        alarms: list[ShanchengControlAlarm],
    ) -> CommandDisposition:
        if not healthy:
            alarms.append(self._alarm(
                channel,
                f"{channel.upper()}_COMMAND_BLOCKED",
                "New request cannot be evaluated while the channel is blocked",
            ))
            return CommandDisposition.BLOCKED
        timeout = (
            self.config.active_timeout_minutes
            if channel == "p" else self.config.reactive_timeout_minutes
        )
        candidate = self._candidate_runtime(
            self._active if channel == "p" else self._reactive,
            request,
            now,
            timeout,
        )
        candidate_active = candidate if channel == "p" else replace(self._active)
        candidate_reactive = candidate if channel == "q" else replace(self._reactive)
        plan = _build_control_plan(
            telemetry,
            self.config,
            capabilities,
            candidate_active,
            candidate_reactive,
        )
        if not plan.feasible:
            other = self._reactive if channel == "p" else self._active
            conflicts_with_retained = False
            if other.state is ShanchengControlMode.DISPATCH_TRACKING:
                local_other = _ChannelRuntime()
                standalone_active = candidate if channel == "p" else local_other
                standalone_reactive = candidate if channel == "q" else local_other
                standalone = _build_control_plan(
                    telemetry,
                    self.config,
                    capabilities,
                    standalone_active,
                    standalone_reactive,
                )
                conflicts_with_retained = standalone.feasible
            alarms.append(self._candidate_alarm(
                channel,
                request,
                plan,
                capabilities,
                conflicts_with_retained,
            ))
            return CommandDisposition.REJECTED
        if channel == "p":
            self._active = candidate
        else:
            self._reactive = candidate
        self._record_accepted(command, channel, request)
        return (
            CommandDisposition.EXITED_TO_LOCAL
            if request.action is ChannelAction.ENTER_LOCAL
            else CommandDisposition.ACCEPTED
        )

    def _commit_atomic_requests(
        self,
        telemetry: ShanchengTelemetry,
        capabilities: ShanchengCapabilityReport,
        command: CommandEnvelope,
        active_request: ActiveRequest,
        reactive_request: ReactiveRequest,
        active_healthy: bool,
        reactive_healthy: bool,
        now: float,
        alarms: list[ShanchengControlAlarm],
    ) -> tuple[CommandDisposition, CommandDisposition]:
        if not active_healthy or not reactive_healthy:
            alarms.append(self._alarm(
                "p", "ATOMIC_COMMAND_BLOCKED",
                "Combined P/Q command cannot be evaluated while a channel is blocked",
            ))
            return CommandDisposition.BLOCKED, CommandDisposition.BLOCKED
        candidate_active = self._candidate_runtime(
            self._active,
            active_request,
            now,
            self.config.active_timeout_minutes,
        )
        candidate_reactive = self._candidate_runtime(
            self._reactive,
            reactive_request,
            now,
            self.config.reactive_timeout_minutes,
        )
        plan = _build_control_plan(
            telemetry,
            self.config,
            capabilities,
            candidate_active,
            candidate_reactive,
        )
        if not plan.feasible:
            alarms.append(self._alarm(
                "p", "JOINT_PQ_INFEASIBLE",
                "Combined P/Q request failed joint proposal validation",
                requested=active_request.target_mw,
            ))
            return CommandDisposition.REJECTED, CommandDisposition.REJECTED
        self._active = candidate_active
        self._reactive = candidate_reactive
        self._record_accepted(command, "p", active_request)
        self._record_accepted(command, "q", reactive_request)
        return (
            CommandDisposition.EXITED_TO_LOCAL
            if active_request.action is ChannelAction.ENTER_LOCAL
            else CommandDisposition.ACCEPTED,
            CommandDisposition.EXITED_TO_LOCAL
            if reactive_request.action is ChannelAction.ENTER_LOCAL
            else CommandDisposition.ACCEPTED,
        )

    def step(
        self,
        telemetry: ShanchengTelemetry,
        command: CommandEnvelope | None = None,
    ) -> ShanchengControlDecision:
        now = telemetry.elapsed_minutes
        if self._last_step_time is not None and now < self._last_step_time:
            raise ValueError("telemetry elapsed_minutes must be monotonic")
        self._last_step_time = now
        capabilities = calculate_shancheng_capabilities(telemetry, self.config)
        alarms: list[ShanchengControlAlarm] = []
        active_disposition = CommandDisposition.NONE
        reactive_disposition = CommandDisposition.NONE

        active_healthy, active_reason = self._channel_health(
            telemetry, capabilities, "p"
        )
        reactive_healthy, reactive_reason = self._channel_health(
            telemetry, capabilities, "q"
        )
        active_was_degraded = (
            self._active.state is ShanchengControlMode.DEGRADED_HOLD
        )
        reactive_was_degraded = (
            self._reactive.state is ShanchengControlMode.DEGRADED_HOLD
        )
        self._apply_health_state(
            self._active, "p", active_healthy, active_reason, alarms
        )
        self._apply_health_state(
            self._reactive, "q", reactive_healthy, reactive_reason, alarms
        )

        self._revalidate_current_plan(telemetry, capabilities, alarms)
        if active_was_degraded:
            self._attempt_recovery_transition(
                telemetry, capabilities, "p", active_healthy
            )
        if reactive_was_degraded:
            self._attempt_recovery_transition(
                telemetry, capabilities, "q", reactive_healthy
            )

        timed_out = self._process_timeout(
            self._active, "p", now, active_healthy, alarms
        )
        if timed_out is not CommandDisposition.NONE:
            active_disposition = timed_out
        timed_out = self._process_timeout(
            self._reactive, "q", now, reactive_healthy, alarms
        )
        if timed_out is not CommandDisposition.NONE:
            reactive_disposition = timed_out

        requests: dict[Literal["p", "q"], ActiveRequest | ReactiveRequest] = {}
        statuses: dict[Literal["p", "q"], str] = {}
        if command is not None:
            if command.active_request is not None:
                requests["p"] = command.active_request
            if command.reactive_request is not None:
                requests["q"] = command.reactive_request
            for channel, request in requests.items():
                statuses[channel] = self._replay_status(command, channel, request)
            freshness = self._freshness_status(command, telemetry.observed_at_utc)
            if freshness != "fresh":
                for channel in requests:
                    if statuses[channel] == "new":
                        statuses[channel] = freshness

            atomic = self.config.same_message_atomic and len(requests) == 2
            protocol_failure = any(status != "new" for status in statuses.values())
            if atomic and protocol_failure:
                for channel, status in statuses.items():
                    if status == "new":
                        status = "atomic"
                    disposition = self._status_disposition(status)
                    if channel == "p":
                        active_disposition = disposition
                    else:
                        reactive_disposition = disposition
                    alarms.append(self._protocol_alarm(channel, status))
            elif atomic:
                active_disposition, reactive_disposition = (
                    self._commit_atomic_requests(
                        telemetry,
                        capabilities,
                        command,
                        command.active_request,
                        command.reactive_request,
                        active_healthy,
                        reactive_healthy,
                        now,
                        alarms,
                    )
                )
            else:
                for channel in ("p", "q"):
                    request = requests.get(channel)
                    if request is None:
                        continue
                    status = statuses[channel]
                    if status != "new":
                        disposition = self._status_disposition(status)
                        alarms.append(self._protocol_alarm(channel, status))
                    else:
                        disposition = self._commit_single_request(
                            telemetry,
                            capabilities,
                            command,
                            channel,
                            request,
                            active_healthy if channel == "p" else reactive_healthy,
                            now,
                            alarms,
                        )
                    if channel == "p":
                        active_disposition = disposition
                    else:
                        reactive_disposition = disposition

        final_plan = _build_control_plan(
            telemetry, self.config, capabilities, self._active, self._reactive
        )
        if not final_plan.feasible:
            self._revalidate_current_plan(telemetry, capabilities, alarms)
            final_plan = _build_control_plan(
                telemetry, self.config, capabilities, self._active, self._reactive
            )
        if not final_plan.feasible:
            raise RuntimeError("infeasible device writes survived the final output gate")
        if (
            active_disposition is CommandDisposition.NONE
            and self._active.state is ShanchengControlMode.DISPATCH_TRACKING
        ):
            active_disposition = CommandDisposition.TRACKING
        elif (
            active_disposition is CommandDisposition.NONE
            and self._active.state is ShanchengControlMode.DEGRADED_HOLD
        ):
            active_disposition = CommandDisposition.SUSPENDED
        elif (
            active_disposition is CommandDisposition.NONE
            and self._active.state in (
                ShanchengControlMode.OUTPUT_BLOCK,
                ShanchengControlMode.RECOVERY_INHIBIT,
            )
        ):
            active_disposition = CommandDisposition.BLOCKED
        if (
            reactive_disposition is CommandDisposition.NONE
            and self._reactive.state is ShanchengControlMode.DISPATCH_TRACKING
        ):
            reactive_disposition = CommandDisposition.TRACKING
        elif (
            reactive_disposition is CommandDisposition.NONE
            and self._reactive.state is ShanchengControlMode.DEGRADED_HOLD
        ):
            reactive_disposition = CommandDisposition.SUSPENDED
        elif (
            reactive_disposition is CommandDisposition.NONE
            and self._reactive.state in (
                ShanchengControlMode.OUTPUT_BLOCK,
                ShanchengControlMode.RECOVERY_INHIBIT,
            )
        ):
            reactive_disposition = CommandDisposition.BLOCKED

        return ShanchengControlDecision(
            active_mode=self._active.state,
            reactive_mode=self._reactive.state,
            active_disposition=active_disposition,
            reactive_disposition=reactive_disposition,
            active_target_mw=self._active.target,
            reactive_target_mvar=self._reactive.target,
            active_suspended_target_mw=self._active.suspended_target,
            reactive_suspended_target_mvar=self._reactive.suspended_target,
            setpoints=final_plan.setpoints,
            capabilities=capabilities,
            alarms=tuple(alarms),
            command_id=command.message_id if command is not None else None,
        )


__all__ = [
    "ActiveMarginReporting", "ActiveRequest", "ChannelAction",
    "ChannelActuationFeedback", "CommandDisposition", "CommandEnvelope",
    "DeviceStatus", "LegacyCommandFactory", "ReactiveRequest", "SignalValidity",
    "ShanchengCapabilityReport", "ShanchengControlAlarm", "ShanchengControlConfig",
    "ShanchengControlDecision", "ShanchengControlMode", "ShanchengController",
    "ShanchengDispatchCommand", "ShanchengSetpoints", "ShanchengTelemetry",
    "StorageInterlocks", "StorageTelemetry", "SvgTelemetry", "WindSetpoint",
    "WindTurbineTelemetry", "calculate_shancheng_capabilities",
    "internal_storage_active_to_southbound", "internal_svg_reactive_to_southbound",
    "internal_wind_active_to_southbound", "internal_wind_reactive_to_southbound",
    "local_shancheng_setpoints", "northbound_active_target_to_internal",
    "northbound_reactive_target_to_internal",
]

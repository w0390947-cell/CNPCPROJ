"""通用安全执行使用的数据可信度、控制意图和现场反馈契约。

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
class HardProtectionStatus:
    """最后一级硬保护的已归一化状态。"""

    active: bool
    validity: SignalValidity

    def __post_init__(self) -> None:
        if not isinstance(self.active, bool):
            raise ValueError("hard protection active must be a boolean")


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
    """Allocation against the supplied PCC snapshot, not achieved device output.

    ``command_accepted`` refers only to a new command submitted in this call;
    no-command observations set it to false. ``fully_allocated`` describes the
    allocation arithmetic, not satisfaction of all retained safety constraints
    or confirmed PCC safety. A later renewal does not refresh this snapshot.
    """

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
    "HardProtectionStatus",
    "PccSafetyConstraint",
    "SafetyAllocationResult",
    "SignalValidity",
    "StationActuationFeedback",
    "StationCapIntent",
]

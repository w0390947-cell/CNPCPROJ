"""D5000工程对接的纯数据契约与PCC量测归一化边界。

这里不实现网络通信，也不内置项目点号。调用方必须提供点表定义、单位、倍率、
方向和新鲜度阈值；任何未知项都会得到无效结果，而不是数值零。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite

from .contracts import MeasurementDirection


class ActivePowerUnit(str, Enum):
    W = "W"
    KW = "kW"
    MW = "MW"

    @property
    def to_mw(self) -> float:
        return {
            ActivePowerUnit.W: 1e-6,
            ActivePowerUnit.KW: 1e-3,
            ActivePowerUnit.MW: 1.0,
        }[self]


class D5000Quality(str, Enum):
    GOOD = "good"
    BAD = "bad"
    QUESTIONABLE = "questionable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class D5000ActivePointDefinition:
    point_id: str
    unit: ActivePowerUnit
    engineering_multiplier: float
    direction: MeasurementDirection

    def __post_init__(self) -> None:
        if not self.point_id.strip():
            raise ValueError("point_id must be non-empty")
        if (
            not isfinite(self.engineering_multiplier)
            or self.engineering_multiplier <= 0.0
        ):
            raise ValueError("engineering_multiplier must be positive and finite")


@dataclass(frozen=True)
class D5000Measurement:
    point_id: str
    raw_value: float
    observed_at_utc: datetime
    quality: D5000Quality


@dataclass(frozen=True)
class NormalizedPccMeasurement:
    p_grid_import_mw: float | None
    valid: bool
    invalid_reasons: tuple[str, ...]
    point_id: str
    observed_at_utc: datetime


@dataclass(frozen=True)
class D5000CommandAcknowledgement:
    request_id: str
    point_id: str
    acknowledged_at_utc: datetime
    transport_accepted: bool
    execution_known: bool
    echoed_raw_value: float | None


class D5000PccAdapter:
    def __init__(
        self,
        definition: D5000ActivePointDefinition,
        *,
        stale_after_seconds: float,
        future_clock_skew_seconds: float,
    ) -> None:
        if not isfinite(stale_after_seconds) or stale_after_seconds <= 0.0:
            raise ValueError("stale_after_seconds must be positive and finite")
        if not isfinite(future_clock_skew_seconds) or future_clock_skew_seconds < 0.0:
            raise ValueError("future_clock_skew_seconds must be nonnegative and finite")
        self.definition = definition
        self.stale_after_seconds = float(stale_after_seconds)
        self.future_clock_skew_seconds = float(future_clock_skew_seconds)

    def normalize(
        self,
        measurement: D5000Measurement,
        *,
        decision_at_utc: datetime,
    ) -> NormalizedPccMeasurement:
        reasons: list[str] = []
        if decision_at_utc.tzinfo is None or measurement.observed_at_utc.tzinfo is None:
            reasons.append("TIMESTAMP_NOT_TIMEZONE_AWARE")
        else:
            decision = decision_at_utc.astimezone(timezone.utc)
            observed = measurement.observed_at_utc.astimezone(timezone.utc)
            age_seconds = (decision - observed).total_seconds()
            if age_seconds < -self.future_clock_skew_seconds:
                reasons.append("TIMESTAMP_FROM_FUTURE")
            if age_seconds > self.stale_after_seconds:
                reasons.append("TELEMETRY_STALE")
        if measurement.point_id != self.definition.point_id:
            reasons.append("POINT_ID_MISMATCH")
        if measurement.quality is not D5000Quality.GOOD:
            reasons.append("QUALITY_NOT_GOOD")
        if not isinstance(measurement.raw_value, (int, float)) or not isfinite(
            float(measurement.raw_value)
        ):
            reasons.append("VALUE_NOT_FINITE")
        if reasons:
            return NormalizedPccMeasurement(
                None, False, tuple(reasons), measurement.point_id,
                measurement.observed_at_utc,
            )
        engineering_value = (
            float(measurement.raw_value) * self.definition.engineering_multiplier
        )
        raw_mw = engineering_value * self.definition.unit.to_mw
        return NormalizedPccMeasurement(
            self.definition.direction.raw_to_internal_import(raw_mw),
            True,
            (),
            measurement.point_id,
            measurement.observed_at_utc,
        )


__all__ = [
    "ActivePowerUnit",
    "D5000ActivePointDefinition",
    "D5000CommandAcknowledgement",
    "D5000Measurement",
    "D5000PccAdapter",
    "D5000Quality",
    "NormalizedPccMeasurement",
]

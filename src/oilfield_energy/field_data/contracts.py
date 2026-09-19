"""与 SCADA/Excel 格式解耦的现场数据契约。

本模块只表达项目资料中能够确认的事实。未知单位、未知拓扑和坏数据均显式保留，
不得用零值或合成参数替代。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class DataSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class DataIssue:
    code: str
    severity: DataSeverity
    message: str
    source: str
    sheet: str | None = None
    cell: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class DataQualityReport:
    source: str
    issue_counts: Mapping[str, int] = field(default_factory=dict)
    issue_samples: tuple[DataIssue, ...] = ()

    @property
    def error_count(self) -> int:
        error_codes = {
            issue.code for issue in self.issue_samples
            if issue.severity is DataSeverity.ERROR
        }
        return sum(self.issue_counts.get(code, 0) for code in error_codes)

    @property
    def warning_count(self) -> int:
        warning_codes = {
            issue.code for issue in self.issue_samples
            if issue.severity is DataSeverity.WARNING
        }
        return sum(self.issue_counts.get(code, 0) for code in warning_codes)

    @property
    def valid(self) -> bool:
        return self.error_count == 0


class MeasurementDirection(str, Enum):
    """原始考核点有功方向到核心统一口径的转换。"""

    BUS_TO_LINE_POSITIVE = "bus_to_line_positive"
    LINE_TO_BUS_POSITIVE = "line_to_bus_positive"

    def raw_to_internal_import(self, raw_value: float) -> float:
        """转换成核心的 ``P_grid > 0`` 表示从线路/上级电网受电。"""
        value = float(raw_value)
        if self is MeasurementDirection.BUS_TO_LINE_POSITIVE:
            return -value
        return value


class AssetKind(str, Enum):
    WIND = "wind"
    PV = "pv"
    STORAGE = "storage"
    SVG = "svg"


@dataclass(frozen=True)
class ControlCapability:
    active_controllable: bool
    reactive_controllable: bool
    q_abs_over_p_max: float | None = None
    q_min_mvar: float | None = None
    q_max_mvar: float | None = None
    provenance: str = ""
    note: str = ""


@dataclass(frozen=True)
class RenewableAsset:
    asset_id: str
    station_name: str
    region_name: str
    kind: AssetKind
    active_capacity_mw: float | None
    energy_capacity_mwh: float | None = None
    capability: ControlCapability | None = None
    provenance: str = ""


@dataclass(frozen=True)
class FieldRegion:
    region_id: str
    display_name: str
    upstream_station: str | None
    provenance: str


@dataclass(frozen=True)
class FieldAssetRegistry:
    regions: tuple[FieldRegion, ...]
    assets: tuple[RenewableAsset, ...]
    pcc_raw_direction: MeasurementDirection
    pv_default_capability: ControlCapability
    wind_default_capability: ControlCapability
    unresolved_requirements: tuple[str, ...]

    def asset(self, asset_id: str) -> RenewableAsset:
        for item in self.assets:
            if item.asset_id == asset_id:
                return item
        raise KeyError(asset_id)


@dataclass(frozen=True)
class ShortCircuitBusRecord:
    row_number: int
    area_name: str | None
    station_name: str | None
    bus_name: str
    positive_sequence_impedance_max_ohm: float | None
    positive_sequence_impedance_min_ohm: float | None
    positive_sequence_capacity_max_raw: float | None
    positive_sequence_capacity_min_raw: float | None
    positive_sequence_current_max_a: float | None
    positive_sequence_current_min_a: float | None
    zero_sequence_impedance_max_ohm: float | None
    zero_sequence_impedance_min_ohm: float | None
    zero_sequence_capacity_max_raw: float | None
    zero_sequence_capacity_min_raw: float | None
    zero_sequence_current_3x_max_raw: float | None
    zero_sequence_current_3x_min_raw: float | None
    maximum_mode_description: str | None
    minimum_mode_description: str | None


@dataclass(frozen=True)
class ShortCircuitWorkbook:
    source: str
    sheet_name: str
    records: tuple[ShortCircuitBusRecord, ...]
    quality: DataQualityReport


@dataclass(frozen=True)
class LoadHistorySheet:
    name: str
    row_count: int
    headers: Mapping[str, str]
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    valid_timestamp_count: int
    invalid_timestamp_count: int


@dataclass(frozen=True)
class LoadHistoryWorkbook:
    source: str
    sheets: tuple[LoadHistorySheet, ...]
    quality: DataQualityReport
    raw_units_confirmed: bool = False
    raw_direction_confirmed: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

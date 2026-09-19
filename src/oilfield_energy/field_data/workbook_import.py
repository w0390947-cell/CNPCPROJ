"""项目方 Excel 的只读导入与质量审计。"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
import re
from typing import Any

from .contracts import (
    DataIssue,
    DataQualityReport,
    DataSeverity,
    LoadHistorySheet,
    LoadHistoryWorkbook,
    ShortCircuitBusRecord,
    ShortCircuitWorkbook,
)
from .xlsx_reader import RawWorksheet, read_xlsx


_CELL_COLUMN = re.compile(r"^[A-Z]+")
_EXCEL_EPOCH = datetime(1899, 12, 30)
_SAMPLE_LIMIT_PER_CODE = 20


class _IssueCollector:
    def __init__(self, source: str) -> None:
        self.source = source
        self.counts: Counter[str] = Counter()
        self.samples: list[DataIssue] = []
        self._sample_counts: Counter[str] = Counter()

    def add(
        self,
        code: str,
        severity: DataSeverity,
        message: str,
        *,
        sheet: str | None = None,
        cell: str | None = None,
        value: Any = None,
    ) -> None:
        self.counts[code] += 1
        if self._sample_counts[code] >= _SAMPLE_LIMIT_PER_CODE:
            return
        self._sample_counts[code] += 1
        self.samples.append(DataIssue(
            code=code,
            severity=severity,
            message=message,
            source=self.source,
            sheet=sheet,
            cell=cell,
            value=None if value is None else str(value),
        ))

    def report(self) -> DataQualityReport:
        return DataQualityReport(
            source=self.source,
            issue_counts=dict(sorted(self.counts.items())),
            issue_samples=tuple(self.samples),
        )


def _column(cell_ref: str) -> str:
    match = _CELL_COLUMN.match(cell_ref)
    if match is None:
        raise ValueError(f"invalid cell reference: {cell_ref}")
    return match.group(0)


def _row_number(cell_ref: str) -> int:
    return int(cell_ref[len(_column(cell_ref)):])


def _row_by_number(sheet: RawWorksheet) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in sheet.rows:
        if not row:
            continue
        number = _row_number(next(iter(row)))
        result[number] = {_column(ref): value for ref, value in row.items()}
    return result


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in {"∞", "+∞", "-∞"}:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def import_short_circuit_workbook(path: str | Path) -> ShortCircuitWorkbook:
    """导入母线等值阻抗和短路数据，保留未给单位的容量为 ``*_raw``。"""
    source = str(Path(path))
    collector = _IssueCollector(source)
    sheets = read_xlsx(path)
    if len(sheets) != 1:
        collector.add(
            "UNEXPECTED_SHEET_COUNT", DataSeverity.ERROR,
            "母线短路工作簿应只有一个数据页。", value=len(sheets),
        )
    if not sheets:
        return ShortCircuitWorkbook(source, "", (), collector.report())
    sheet = sheets[0]
    rows = _row_by_number(sheet)
    headers = rows.get(4, {})
    for column in ("G", "H", "O", "P", "Q", "R"):
        collector.add(
            "ENGINEERING_UNIT_MISSING",
            DataSeverity.ERROR,
            "短路容量/零序电流字段未标注工程单位，禁止自动换算。",
            sheet=sheet.name,
            cell=f"{column}4",
            value=headers.get(column),
        )

    records: list[ShortCircuitBusRecord] = []
    current_area: str | None = None
    current_station: str | None = None
    for row_number in range(6, max(rows, default=5) + 1):
        row = rows.get(row_number, {})
        if _as_text(row.get("B")) is not None:
            current_area = _as_text(row.get("B"))
        if _as_text(row.get("C")) is not None:
            current_station = _as_text(row.get("C"))
        bus_name = _as_text(row.get("D"))
        if bus_name is None:
            continue
        numeric_columns = ("E", "F", "G", "H", "I", "J", "M", "N", "O", "P", "Q", "R")
        for column in numeric_columns:
            value = row.get(column)
            if value is not None and _as_float(value) is None and str(value).strip() not in {"∞", "+∞", "-∞"}:
                collector.add(
                    "NONNUMERIC_SHORT_CIRCUIT_VALUE", DataSeverity.ERROR,
                    "短路数据字段不是有限数值。", sheet=sheet.name,
                    cell=f"{column}{row_number}", value=value,
                )
        records.append(ShortCircuitBusRecord(
            row_number=row_number,
            area_name=current_area,
            station_name=current_station,
            bus_name=bus_name,
            positive_sequence_impedance_max_ohm=_as_float(row.get("E")),
            positive_sequence_impedance_min_ohm=_as_float(row.get("F")),
            positive_sequence_capacity_max_raw=_as_float(row.get("G")),
            positive_sequence_capacity_min_raw=_as_float(row.get("H")),
            positive_sequence_current_max_a=_as_float(row.get("I")),
            positive_sequence_current_min_a=_as_float(row.get("J")),
            zero_sequence_impedance_max_ohm=_as_float(row.get("M")),
            zero_sequence_impedance_min_ohm=_as_float(row.get("N")),
            zero_sequence_capacity_max_raw=_as_float(row.get("O")),
            zero_sequence_capacity_min_raw=_as_float(row.get("P")),
            zero_sequence_current_3x_max_raw=_as_float(row.get("Q")),
            zero_sequence_current_3x_min_raw=_as_float(row.get("R")),
            maximum_mode_description=_as_text(row.get("K")),
            minimum_mode_description=_as_text(row.get("L")),
        ))
    if not records:
        collector.add(
            "NO_SHORT_CIRCUIT_RECORDS", DataSeverity.ERROR,
            "没有找到第6行起的母线记录。", sheet=sheet.name,
        )
    return ShortCircuitWorkbook(source, sheet.name, tuple(records), collector.report())


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not isfinite(number) or number < 1.0:
            return None
        return _EXCEL_EPOCH + timedelta(days=number)
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None and isfinite(number) and number >= 1.0:
        return _EXCEL_EPOCH + timedelta(days=number)
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _measurement_kind(header: str) -> str:
    if "功率因数" in header:
        return "power_factor"
    if "有功" in header:
        return "active_power"
    if "无功" in header:
        return "reactive_power"
    if "电流" in header:
        return "current"
    if "电压" in header:
        return "voltage"
    return "unknown"


def audit_line_load_workbook(
    path: str | Path,
    *,
    expected_interval: timedelta = timedelta(hours=1),
) -> LoadHistoryWorkbook:
    """审计线路历史量测；不猜单位、不改符号、不填补异常值。"""
    source = str(Path(path))
    collector = _IssueCollector(source)
    sheets = read_xlsx(path)
    summaries: list[LoadHistorySheet] = []
    for sheet in sheets:
        numbered = _row_by_number(sheet)
        header_row = numbered.get(1, {})
        headers = {
            column: text
            for column, value in header_row.items()
            if (text := _as_text(value)) is not None
        }
        if headers.get("A") != "时间":
            collector.add(
                "TIMESTAMP_HEADER_INVALID", DataSeverity.ERROR,
                "A列应为时间字段。", sheet=sheet.name, cell="A1", value=headers.get("A"),
            )
        kinds = {column: _measurement_kind(header) for column, header in headers.items() if column != "A"}
        for column, kind in kinds.items():
            if kind == "unknown":
                collector.add(
                    "MEASUREMENT_KIND_UNKNOWN", DataSeverity.WARNING,
                    "无法从表头识别量测类型。", sheet=sheet.name,
                    cell=f"{column}1", value=headers[column],
                )
            collector.add(
                "ENGINEERING_UNIT_MISSING", DataSeverity.ERROR,
                "量测表头未给出工程单位/倍率，禁止自动换算为MW、Mvar、kV或A。",
                sheet=sheet.name, cell=f"{column}1", value=headers[column],
            )

        timestamps: list[datetime] = []
        invalid_timestamps = 0
        zero_runs: dict[str, tuple[int, int]] = {}
        for row_number in range(2, max(numbered, default=1) + 1):
            row = numbered.get(row_number, {})
            timestamp = _parse_timestamp(row.get("A"))
            if timestamp is None:
                invalid_timestamps += 1
                collector.add(
                    "TIMESTAMP_INVALID", DataSeverity.ERROR,
                    "时间字段无法按Excel序列或支持的日期格式解析。",
                    sheet=sheet.name, cell=f"A{row_number}", value=row.get("A"),
                )
            else:
                if timestamps:
                    delta = timestamp - timestamps[-1]
                    if abs(delta - expected_interval) > timedelta(seconds=1):
                        code = "TIMESTAMP_DUPLICATE_OR_REVERSED" if delta <= timedelta(0) else "TIMESTAMP_GAP"
                        collector.add(
                            code, DataSeverity.ERROR,
                            "相邻有效时间戳不满足预期1小时间隔。",
                            sheet=sheet.name, cell=f"A{row_number}", value=delta,
                        )
                timestamps.append(timestamp)

            for column, kind in kinds.items():
                value = row.get(column)
                number = _as_float(value)
                if number is None:
                    if value is not None:
                        collector.add(
                            "MEASUREMENT_NONNUMERIC", DataSeverity.ERROR,
                            "量测字段不是有限数值。", sheet=sheet.name,
                            cell=f"{column}{row_number}", value=value,
                        )
                    zero_runs.pop(column, None)
                    continue
                if kind == "power_factor" and abs(number) > 1.0 + 1e-9:
                    collector.add(
                        "POWER_FACTOR_OUT_OF_RANGE", DataSeverity.ERROR,
                        "功率因数绝对值超过1。", sheet=sheet.name,
                        cell=f"{column}{row_number}", value=number,
                    )
                if kind == "voltage" and number <= 0.0:
                    collector.add(
                        "NONPOSITIVE_VOLTAGE", DataSeverity.ERROR,
                        "电压量测非正，不能直接用于潮流校准。", sheet=sheet.name,
                        cell=f"{column}{row_number}", value=number,
                    )
                if abs(number) <= 1e-12:
                    start, length = zero_runs.get(column, (row_number, 0))
                    zero_runs[column] = (start, length + 1)
                else:
                    start_length = zero_runs.pop(column, None)
                    if start_length is not None and start_length[1] >= 6:
                        collector.add(
                            "LONG_ZERO_RUN", DataSeverity.WARNING,
                            "连续至少6小时为零；保留原值，但需区分停运、缺测和真实零值。",
                            sheet=sheet.name, cell=f"{column}{start_length[0]}",
                            value=start_length[1],
                        )
        for column, start_length in zero_runs.items():
            if start_length[1] >= 6:
                collector.add(
                    "LONG_ZERO_RUN", DataSeverity.WARNING,
                    "连续至少6小时为零；保留原值，但需区分停运、缺测和真实零值。",
                    sheet=sheet.name, cell=f"{column}{start_length[0]}", value=start_length[1],
                )
        summaries.append(LoadHistorySheet(
            name=sheet.name,
            row_count=max(0, len(numbered) - 1),
            headers=headers,
            first_timestamp=timestamps[0] if timestamps else None,
            last_timestamp=timestamps[-1] if timestamps else None,
            valid_timestamp_count=len(timestamps),
            invalid_timestamp_count=invalid_timestamps,
        ))
    if not sheets:
        collector.add("NO_WORKSHEETS", DataSeverity.ERROR, "工作簿没有工作表。")
    return LoadHistoryWorkbook(
        source=source,
        sheets=tuple(summaries),
        quality=collector.report(),
        raw_units_confirmed=False,
        raw_direction_confirmed=False,
        metadata={
            "expected_interval_minutes": expected_interval.total_seconds() / 60.0,
            "conversion_performed": False,
        },
    )


__all__ = ["audit_line_load_workbook", "import_short_circuit_workbook"]

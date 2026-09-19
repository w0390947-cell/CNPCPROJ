"""Explicit field records -> fixed device snapshot -> AC power flow, without dispatch.

The manifest is a versioned declaration of topology, inventory, metering scope and
conversion rules. It is never inferred from workbook headings or synthetic data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from pydantic import TypeAdapter

from ..network_model import NetworkModelV2, assess_network_model
from ..network_scenarios import NetworkSecurityLimits, evaluate_operating_point
from ..power_flow_comparison import (
    BusLoadState, DeviceState, FixedStateSnapshot, _capability_violations,
    _json_default, _reject_extra_keys, build_operating_point,
)
from ..resource_control_contracts import ResourceType
from .xlsx_reader import read_xlsx


@dataclass(frozen=True)
class MeasurementSource:
    source_id: str
    path: str
    format: Literal["csv", "xlsx"]
    point_column: str
    time_column: str
    value_column: str
    quality_column: str
    good_quality: str
    timestamp_format: str  # iso8601, excel1900, or explicit strptime format
    utc_offset_minutes: int  # applied only to naive timestamps
    sheet: str = ""
    header_row: int = 1


@dataclass(frozen=True)
class FieldDevice:
    resource_id: str
    bus_id: str
    resource_type: ResourceType
    p_min_mw: float | None = None
    p_max_mw: float | None = None
    q_min_mvar: float | None = None
    q_max_mvar: float | None = None
    s_max_mva: float | None = None


@dataclass(frozen=True)
class FieldLoad:
    load_id: str
    bus_id: str
    scope_members: tuple[str, ...]
    basis: Literal["gross_load"]


@dataclass(frozen=True)
class FieldPoint:
    point_id: str
    source_id: str
    target_kind: Literal["device", "load", "boundary"]
    target_id: str
    quantity: Literal["p", "q", "in_service", "voltage"]
    unit: Literal["W", "kW", "MW", "var", "kvar", "Mvar", "V", "kV", "pu", "bool"]
    multiplier: float
    positive_direction: Literal["injection", "withdrawal", "consumption", "supply", "magnitude", "on"]
    sample_kind: Literal["instantaneous"]


@dataclass(frozen=True)
class FieldSnapshotRequest:
    schema_version: Literal["field-snapshot-v1"]
    dataset_id: str
    source: str
    synthetic: bool
    inventory_complete: bool
    mapping_confirmed: bool
    metering_scopes_confirmed: bool
    network: NetworkModelV2
    operating_mode_id: str
    operating_mode_source: str
    operating_mode_valid_from: datetime
    operating_mode_valid_until: datetime
    at: datetime
    max_age_seconds: float
    max_skew_seconds: float
    sources: tuple[MeasurementSource, ...]
    devices: tuple[FieldDevice, ...]
    loads: tuple[FieldLoad, ...]
    zero_load_bus_ids: tuple[str, ...]
    points: tuple[FieldPoint, ...]
    limits: NetworkSecurityLimits


class SnapshotInputError(ValueError):
    """A missing or ambiguous input cannot be converted into a physical zero."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotInputError(message)


def _unique(values, label: str) -> None:
    values = list(values)
    _require(all(isinstance(v, str) and v.strip() for v in values), f"EMPTY_ID:{label}")
    _require(len(values) == len(set(values)), f"DUPLICATE:{label}")


def read_snapshot_request(path: Path) -> FieldSnapshotRequest:
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, f"DUPLICATE_JSON_KEY:{key}")
            result[key] = value
        return result
    raw = path.read_text(encoding="utf-8-sig")
    value = json.loads(raw, object_pairs_hook=pairs)
    _reject_extra_keys(value, FieldSnapshotRequest)
    return TypeAdapter(FieldSnapshotRequest).validate_json(raw, strict=True)


def _validate(request: FieldSnapshotRequest, demo: bool):
    r = request
    _require(r.schema_version == "field-snapshot-v1", "SCHEMA_VERSION_UNSUPPORTED")
    _require(all(x.strip() for x in (r.dataset_id, r.source, r.operating_mode_source)), "PROVENANCE_MISSING")
    _require(r.synthetic == r.network.provenance.synthetic, "SYNTHETIC_PROVENANCE_MISMATCH")
    _require(not r.synthetic or demo, "SYNTHETIC_INPUT_REQUIRES_EXPLICIT_DEMO")
    _require(not demo or r.synthetic, "DEMO_REQUIRES_SYNTHETIC_INPUT")
    _require(r.inventory_complete and r.mapping_confirmed and r.metering_scopes_confirmed,
             "INVENTORY_MAPPING_OR_METERING_SCOPE_UNCONFIRMED")
    for label, value in (("at", r.at), ("mode_from", r.operating_mode_valid_from),
                         ("mode_until", r.operating_mode_valid_until)):
        _require(value.utcoffset() is not None, f"TIMEZONE_REQUIRED:{label}")
    _require(r.operating_mode_valid_from <= r.at < r.operating_mode_valid_until,
             "OPERATING_MODE_NOT_VALID_AT_TARGET")
    for value in (r.max_age_seconds, r.max_skew_seconds):
        _require(not isinstance(value, bool) and isfinite(value) and value >= 0, "INVALID_TIME_TOLERANCE")
    ready = assess_network_model(r.network, r.operating_mode_id, require_field_approval=not demo)
    _require(ready.ready_for_current_solver and (demo or ready.ready_for_field_case),
             "NETWORK_NOT_READY:" + ";".join(ready.blockers))
    _require(all(b.nominal_voltage_kv is not None for b in r.network.buses), "BUS_NOMINAL_VOLTAGE_MISSING")
    buses = {b.bus_id for b in r.network.buses}
    _unique((s.source_id for s in r.sources), "source_id")
    _unique((d.resource_id for d in r.devices), "resource_id")
    _require(not {d.resource_id for d in r.devices}.intersection(s.shunt_id for s in r.network.shunts),
             "DEVICE_AND_NETWORK_SHUNT_DOUBLE_COUNT")
    _unique((l.load_id for l in r.loads), "load_id")
    _unique(r.zero_load_bus_ids, "zero_load_bus_ids")
    _require(all(d.bus_id in buses for d in r.devices), "DEVICE_BUS_UNKNOWN")
    _require(all(l.bus_id in buses and l.basis == "gross_load" for l in r.loads), "LOAD_BUS_OR_BASIS_INVALID")
    load_buses = {l.bus_id for l in r.loads}
    _require(not load_buses.intersection(r.zero_load_bus_ids), "ZERO_AND_MEASURED_LOAD_OVERLAP")
    _require(load_buses.union(r.zero_load_bus_ids) == buses, "LOAD_BUS_COVERAGE_INCOMPLETE")
    _require(all(l.scope_members for l in r.loads), "LOAD_SCOPE_MEMBERS_MISSING")
    _unique((member for l in r.loads for member in l.scope_members), "load_scope_member")
    expected = {("device", d.resource_id, q) for d in r.devices for q in ("p", "q", "in_service")}
    expected |= {("load", l.load_id, q) for l in r.loads for q in ("p", "q")}
    expected.add(("boundary", r.network.pcc_bus_id, "voltage"))
    targets = [(p.target_kind, p.target_id, p.quantity) for p in r.points]
    _require(len(targets) == len(set(targets)), "DUPLICATE_TARGET_QUANTITY")
    _require(set(targets) == expected, "POINT_COVERAGE_MISMATCH")
    point_keys = [(p.source_id, p.point_id) for p in r.points]
    _require(len(point_keys) == len(set(point_keys)), "DUPLICATE_POINT_MAPPING")
    source_ids = {s.source_id for s in r.sources}
    for p in r.points:
        _require(bool(p.point_id.strip()) and p.source_id in source_ids, "POINT_SOURCE_UNKNOWN")
        _require(p.sample_kind == "instantaneous", "NON_INSTANTANEOUS_MEASUREMENT")
        _require(not isinstance(p.multiplier, bool) and isfinite(p.multiplier) and p.multiplier > 0,
                 f"INVALID_MULTIPLIER:{p.point_id}")
        units = {"p": {"W", "kW", "MW"}, "q": {"var", "kvar", "Mvar"},
                 "in_service": {"bool"}, "voltage": {"V", "kV", "pu"}}
        directions = {"device": {"injection", "withdrawal"}, "load": {"consumption", "supply"}}
        allowed = {"on"} if p.quantity == "in_service" else {"magnitude"} if p.quantity == "voltage" else directions[p.target_kind]
        _require(p.unit in units[p.quantity] and p.positive_direction in allowed, f"UNIT_OR_DIRECTION_INVALID:{p.point_id}")
        _require(p.quantity != "in_service" or p.multiplier == 1, "STATUS_MULTIPLIER_MUST_BE_ONE")
    return ready


def _records(source: MeasurementSource, base: Path) -> list[dict]:
    _require(-1439 <= source.utc_offset_minutes <= 1439, "UTC_OFFSET_INVALID")
    _require(source.header_row >= 1, "HEADER_ROW_INVALID")
    _require(bool(source.good_quality.strip()) and bool(source.timestamp_format.strip()), "SOURCE_RULE_MISSING")
    columns = (source.point_column, source.time_column, source.value_column, source.quality_column)
    _unique(columns, "source_columns")
    path = base / source.path
    if source.format == "csv":
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        _require(len(rows) >= source.header_row, f"HEADER_MISSING:{source.source_id}")
        headers = rows[source.header_row - 1]
        raw = [(i, row) for i, row in enumerate(rows[source.header_row:], source.header_row + 1) if any(row)]
        _require(all(len(row) == len(headers) for _, row in raw), f"CSV_ROW_WIDTH_INVALID:{source.source_id}")
        data = [(i, dict(zip(headers, row))) for i, row in raw]
    else:
        try:
            sheets = [s for s in read_xlsx(path) if s.name == source.sheet]
        except (KeyError, IndexError, ParseError, BadZipFile) as exc:
            raise SnapshotInputError(f"WORKBOOK_INVALID:{source.source_id}:{exc}") from exc
        _require(len(sheets) == 1, f"SHEET_NOT_FOUND:{source.sheet}")
        cells = {key: value for row in sheets[0].rows for key, value in row.items()}
        header_cells = {key.rstrip("0123456789"): str(value) for key, value in cells.items()
                        if int(key.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) == source.header_row and value is not None}
        headers = list(header_cells.values())
        indices = sorted({int(key.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) for key in cells
                          if int(key.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) > source.header_row})
        data = [(i, {name: cells.get(f"{col}{i}") for col, name in header_cells.items()}) for i in indices]
        data = [(i, row) for i, row in data if any(v is not None for v in row.values())]
    _unique(headers, "table_headers")
    _require(set(columns).issubset(headers), f"SOURCE_COLUMNS_MISSING:{source.source_id}")
    return [dict(source_id=source.source_id, row_number=i, sheet=source.sheet,
                 point_id=str(row[source.point_column]), raw_time=row[source.time_column],
                 raw_value=row[source.value_column], quality=str(row[source.quality_column])) for i, row in data]


def _timestamp(raw, source: MeasurementSource) -> datetime:
    try:
        if source.timestamp_format == "iso8601":
            result = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        elif source.timestamp_format == "excel1900":
            value = float(raw)
            _require(isfinite(value) and value >= 61, "INVALID_EXCEL_DATE")
            result = datetime(1899, 12, 30) + timedelta(days=value)
        else:
            result = datetime.strptime(str(raw), source.timestamp_format)
        return result if result.utcoffset() is not None else result.replace(
            tzinfo=timezone(timedelta(minutes=source.utc_offset_minutes)))
    except (ValueError, TypeError, OverflowError) as exc:
        raise SnapshotInputError(f"TIMESTAMP_INVALID:{source.source_id}:{raw}") from exc


def build_field_snapshot(request: FieldSnapshotRequest, base: Path, *, demo: bool = False,
                         audit: list[dict[str, object]] | None = None) -> FixedStateSnapshot:
    """Use latest causal sample within tolerance; a bad latest sample never falls back."""
    _validate(request, demo)
    audit = audit if audit is not None else []
    sources = {s.source_id: s for s in request.sources}
    tables = {s.source_id: _records(s, base) for s in request.sources}
    selected = {}
    times = []
    for point in request.points:
        source = sources[point.source_id]
        records = [row for row in tables[point.source_id] if row["point_id"] == point.point_id]
        timed = [(_timestamp(row["raw_time"], source), row) for row in records]
        _require(len({at for at, _ in timed}) == len(timed), f"DUPLICATE_POINT_TIMESTAMP:{point.point_id}")
        candidates = [(at, row) for at, row in timed if at <= request.at]
        _require(bool(candidates), f"MISSING_CAUSAL_MEASUREMENT:{point.point_id}")
        at, row = max(candidates, key=lambda pair: pair[0])
        evidence = dict(row, observed_at=at, target_kind=point.target_kind, target_id=point.target_id,
                        quantity=point.quantity, unit=point.unit, multiplier=point.multiplier,
                        positive_direction=point.positive_direction, normalized_value=None)
        audit.append(evidence)
        if isinstance(row["raw_value"], float) and not isfinite(row["raw_value"]):
            evidence["raw_value"] = str(row["raw_value"])
        _require((request.at - at).total_seconds() <= request.max_age_seconds, f"STALE_MEASUREMENT:{point.point_id}")
        _require(row["quality"] == source.good_quality, f"QUALITY_NOT_GOOD:{point.point_id}")
        try:
            _require(point.quantity == "in_service" or not isinstance(row["raw_value"], bool),
                     f"BOOLEAN_NUMERIC_VALUE:{point.point_id}")
            value = float(row["raw_value"])
        except (TypeError, ValueError) as exc:
            raise SnapshotInputError(f"MEASUREMENT_NONNUMERIC:{point.point_id}") from exc
        _require(isfinite(value), f"MEASUREMENT_NONFINITE:{point.point_id}")
        if point.quantity == "in_service":
            _require(value in (0, 1), f"INVALID_DEVICE_STATUS:{point.point_id}")
            normalized = bool(value)
        else:
            scale = {"W": 1e-6, "kW": 1e-3, "MW": 1, "var": 1e-6, "kvar": 1e-3,
                     "Mvar": 1, "V": 1e-3, "kV": 1, "pu": 1}[point.unit]
            normalized = value * point.multiplier * scale
            if point.positive_direction in ("withdrawal", "supply"):
                normalized *= -1
            if point.quantity == "voltage" and point.unit != "pu":
                nominal = next(b.nominal_voltage_kv for b in request.network.buses if b.bus_id == request.network.pcc_bus_id)
                normalized /= nominal
            _require(isfinite(normalized), f"CONVERSION_NONFINITE:{point.point_id}")
        evidence["normalized_value"] = normalized
        selected[point.target_kind, point.target_id, point.quantity] = normalized
        times.append(at)
    _require((max(times) - min(times)).total_seconds() <= request.max_skew_seconds, "SNAPSHOT_NOT_COHERENT")
    loads = {b.bus_id: [0.0, 0.0] for b in request.network.buses}
    for load in request.loads:
        p = selected["load", load.load_id, "p"]
        _require(p >= 0, f"GROSS_LOAD_NEGATIVE:{load.load_id}")
        loads[load.bus_id][0] += p
        loads[load.bus_id][1] += selected["load", load.load_id, "q"]
    devices = tuple(DeviceState(**asdict(d), p_mw=selected["device", d.resource_id, "p"],
                                q_mvar=selected["device", d.resource_id, "q"],
                                in_service=selected["device", d.resource_id, "in_service"]) for d in request.devices)
    return FixedStateSnapshot(
        snapshot_id=f"{request.dataset_id}@{request.at.isoformat()}", at=request.at,
        source=request.source, load_basis="gross_bus_load",
        slack_voltage_pu=selected["boundary", request.network.pcc_bus_id, "voltage"],
        quality_valid=True, coherent=True,
        loads=tuple(BusLoadState(bus, *pq) for bus, pq in loads.items()), devices=devices,
    )


def calculate_field_power_flow(request: FieldSnapshotRequest, base: Path, *, demo: bool = False) -> dict[str, object]:
    report = dict(scope="field_fixed_snapshot_ac_power_flow", synthetic=request.synthetic,
                  dataset_id=request.dataset_id, source=request.source, target_at=request.at,
                  network_id=request.network.network_id,
                  network_version=request.network.provenance.dataset_version,
                  operating_mode_id=request.operating_mode_id,
                  operating_mode_source=request.operating_mode_source,
                  field_acceptance_certified=False, redispatch_performed=False,
                  status="invalid_input", reasons=[], selected_measurements=[], snapshot=None,
                  operating_point=None, flow=None, device_violations=[], pcc_direction=None)
    try:
        snapshot = build_field_snapshot(request, base, demo=demo, audit=report["selected_measurements"])
        report["snapshot"] = asdict(snapshot)
        point = build_operating_point(snapshot, request.network)
        report["operating_point"] = dict(point_id=point.point_id, source=point.source,
                                         p_demand_mw_by_bus=dict(point.p_demand_mw_by_bus),
                                         q_demand_mvar_by_bus=dict(point.q_demand_mvar_by_bus),
                                         quality_valid=point.quality_valid, coherent=point.coherent)
        ready = assess_network_model(request.network, request.operating_mode_id, require_field_approval=not demo)
        report["network_readiness"] = dict(warnings=list(ready.warnings), blockers=list(ready.blockers))
        flow = evaluate_operating_point(ready.require_current_solver_ready(), point, request.limits,
                                        slack_voltage_pu=snapshot.slack_voltage_pu)
        report.update(flow=asdict(flow), status=flow.status.value, reasons=list(flow.reasons))
        report["device_violations"] = _capability_violations(snapshot, request.limits.comparison_tolerance)
        if report["status"] in ("secure", "violation"):
            if report["device_violations"]:
                report["status"] = "violation"
            p = flow.pcc_import_mw
            eps = request.limits.comparison_tolerance
            report["pcc_direction"] = "import" if p > eps else "export" if p < -eps else "zero_exchange"
    except (ValueError, OSError, OverflowError) as exc:
        report.update(status="invalid_input", reasons=[str(exc)])
    return report


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False,
                               default=_json_default), encoding="utf-8")


def write_snapshot_outputs(output: Path, report: dict[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "power_flow.json", report)
    _write_json(output / "snapshot.json", report.get("snapshot"))
    for name, fallback in (("buses", "bus_id"), ("branches", "branch_id"), ("system", "quantity")):
        flow = report.get("flow") or {}
        rows = flow.get(name, [])
        if name == "system":
            rows = [dict(quantity=k, value=flow[k]) for k in
                    ("pcc_import_mw", "pcc_reactive_mvar", "power_factor", "loss_mw", "reactive_loss_mvar") if flow.get(k) is not None]
        with (output / f"{name}.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else [fallback])
            writer.writeheader()
            writer.writerows(rows)


def run_field_power_flow_file(input_path: Path, output: Path, *, demo: bool = False, at: str | None = None) -> int:
    """Archive the exact files used plus a replayable manifest, including failed runs."""
    output.mkdir(parents=True, exist_ok=True)
    report = dict(status="invalid_input", reasons=[], redispatch_performed=False,
                  field_acceptance_certified=False, snapshot=None, flow=None)
    archived = []
    try:
        original_manifest = input_path.read_bytes()
        (output / "original_manifest.json").write_bytes(original_manifest)
        request = read_snapshot_request(input_path)
        if at is not None:
            request = replace(request, at=datetime.fromisoformat(at.replace("Z", "+00:00")))
        _validate(request, demo)
        # Read all bytes before writing, so replaying into the same directory works.
        originals = [(s, (input_path.parent / s.path).read_bytes()) for s in request.sources]
        sources = []
        (output / "inputs").mkdir(exist_ok=True)
        for index, (source, payload) in enumerate(originals):
            relative = f"inputs/source_{index}.{source.format}"
            (output / relative).write_bytes(payload)
            sources.append(replace(source, path=relative))
            archived.append(dict(source_id=source.source_id, original_path=source.path,
                                 archived_path=relative, sha256=hashlib.sha256(payload).hexdigest()))
        request = replace(request, sources=tuple(sources))
        _write_json(output / "request.json", asdict(request))
        report = calculate_field_power_flow(request, output, demo=demo)
    except (ValueError, OSError, OverflowError) as exc:
        report["reasons"] = [str(exc)]
        _write_json(output / "request.json", None)
    report["input_archives"] = archived
    write_snapshot_outputs(output, report)
    print(f"Field snapshot power flow: {report['status']}; redispatch=False")
    print(f"Results written to: {output.resolve()}")
    return 0 if report["status"] in ("secure", "violation") else 2

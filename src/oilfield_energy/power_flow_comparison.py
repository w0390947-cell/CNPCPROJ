"""Single-device fixed-state AC comparisons, with no optimization or redispatch.

Inputs are normalized gross bus loads plus independent device injections. This
module does not infer field point mappings, net-meter boundaries, or missing data.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import datetime
from enum import Enum
from math import hypot, isfinite
from pathlib import Path
from typing import ClassVar, Literal, get_args, get_type_hints

from pydantic import ConfigDict, TypeAdapter

from .network_model import NetworkModelV2, assess_network_model
from .network_scenarios import (
    NetworkOperatingPoint, NetworkSecurityLimits, PointFlowResult, ScenarioStatus,
    evaluate_operating_point,
)
from .resource_control_contracts import ResourceType


def _number(value: float, name: str) -> None:
    if isinstance(value, (bool, str)) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _identity(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")


@dataclass(frozen=True)
class DeviceState:
    """P/Q positive into the network; storage charging is negative P.

Optional capability bounds are reported, never used to clip the requested state.
"""

    __pydantic_config__: ClassVar = ConfigDict(extra="forbid")
    resource_id: str
    bus_id: str
    resource_type: ResourceType
    p_mw: float
    q_mvar: float
    in_service: bool
    p_min_mw: float | None = None
    p_max_mw: float | None = None
    q_min_mvar: float | None = None
    q_max_mvar: float | None = None
    s_max_mva: float | None = None

    def __post_init__(self) -> None:
        _identity(self.resource_id, "resource_id")
        _identity(self.bus_id, "bus_id")
        if not isinstance(self.resource_type, ResourceType) or type(self.in_service) is not bool:
            raise ValueError("resource_type and in_service must be explicit typed values")
        for name in ("p_mw", "q_mvar", "p_min_mw", "p_max_mw", "q_min_mvar", "q_max_mvar", "s_max_mva"):
            value = getattr(self, name)
            if value is not None:
                _number(value, name)
        if self.p_mw is None or self.q_mvar is None:
            raise ValueError("device P/Q cannot be missing")
        _number(hypot(self.p_mw, self.q_mvar), "device apparent power")
        if not self.in_service and (self.p_mw != 0 or self.q_mvar != 0):
            raise ValueError("out-of-service devices must explicitly have zero P/Q")
        for low, high in ((self.p_min_mw, self.p_max_mw), (self.q_min_mvar, self.q_max_mvar)):
            if low is not None and high is not None and low > high:
                raise ValueError("capability lower bound exceeds upper bound")
        if self.s_max_mva is not None and self.s_max_mva <= 0:
            raise ValueError("s_max_mva must be positive")


@dataclass(frozen=True)
class BusLoadState:
    __pydantic_config__: ClassVar = ConfigDict(extra="forbid")
    bus_id: str
    p_mw: float
    q_mvar: float

    def __post_init__(self) -> None:
        _identity(self.bus_id, "bus_id")
        _number(self.p_mw, "load P")
        _number(self.q_mvar, "load Q")
        if self.p_mw < 0:
            raise ValueError("gross load P must be nonnegative; model generation as a device")


@dataclass(frozen=True)
class FixedStateSnapshot:
    __pydantic_config__: ClassVar = ConfigDict(extra="forbid")
    snapshot_id: str
    at: datetime
    source: str
    load_basis: Literal["gross_bus_load"]
    slack_voltage_pu: float
    quality_valid: bool
    coherent: bool
    loads: tuple[BusLoadState, ...]
    devices: tuple[DeviceState, ...]

    def __post_init__(self) -> None:
        _identity(self.snapshot_id, "snapshot_id")
        _identity(self.source, "source")
        if not isinstance(self.at, datetime) or self.at.utcoffset() is None:
            raise ValueError("snapshot timestamp must include a timezone")
        if self.load_basis != "gross_bus_load":
            raise ValueError("only explicitly identified gross bus loads are supported")
        _number(self.slack_voltage_pu, "slack_voltage_pu")
        if self.slack_voltage_pu <= 0:
            raise ValueError("slack_voltage_pu must be positive")
        if type(self.quality_valid) is not bool or type(self.coherent) is not bool:
            raise ValueError("quality and coherence must be explicit booleans")
        object.__setattr__(self, "loads", tuple(self.loads))
        object.__setattr__(self, "devices", tuple(self.devices))
        if len({x.bus_id for x in self.loads}) != len(self.loads):
            raise ValueError("each gross bus load must appear exactly once")
        if len({x.resource_id for x in self.devices}) != len(self.devices):
            raise ValueError("device IDs must be unique, even on the same bus")


@dataclass(frozen=True)
class DevicePerturbation:
    __pydantic_config__: ClassVar = ConfigDict(extra="forbid")
    resource_id: str
    quantity: Literal["p_mw", "q_mvar"]
    delta: float

    def __post_init__(self) -> None:
        _identity(self.resource_id, "resource_id")
        if self.quantity not in ("p_mw", "q_mvar"):
            raise ValueError("quantity must be p_mw or q_mvar; the other quantity stays fixed")
        _number(self.delta, "delta")


@dataclass(frozen=True)
class ComparisonRequest:
    __pydantic_config__: ClassVar = ConfigDict(extra="forbid")
    schema_version: Literal["fixed-device-comparison-v1"]
    network: NetworkModelV2
    operating_mode_id: str
    snapshot: FixedStateSnapshot
    perturbation: DevicePerturbation
    limits: NetworkSecurityLimits
    require_field_approval: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != "fixed-device-comparison-v1":
            raise ValueError("unsupported comparison schema version")
        _identity(self.operating_mode_id, "operating_mode_id")
        if type(self.require_field_approval) is not bool:
            raise ValueError("require_field_approval must be boolean")


def _reject_extra_keys(value, annotation, path="request") -> None:
    """Reject typos also in existing network dataclasses (which allow extras)."""
    args = get_args(annotation)
    if is_dataclass(annotation) and isinstance(value, dict):
        names = {f.name for f in fields(annotation)}
        extra = set(value) - names
        if extra:
            raise ValueError(f"{path}: unknown fields {sorted(extra)}")
        hints = get_type_hints(annotation)
        for key, item in value.items():
            _reject_extra_keys(item, hints[key], f"{path}.{key}")
    elif isinstance(value, list) and args:
        for index, item in enumerate(value):
            _reject_extra_keys(item, args[0], f"{path}[{index}]")
    elif isinstance(value, dict):
        for candidate in args:
            if is_dataclass(candidate):
                _reject_extra_keys(value, candidate, path)


def read_comparison_request(path: Path) -> ComparisonRequest:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    raw = path.read_text(encoding="utf-8-sig")
    value = json.loads(raw, object_pairs_hook=unique_object)
    _reject_extra_keys(value, ComparisonRequest)
    # Strict JSON accepts date strings and tuple arrays but rejects numeric strings/bools.
    return TypeAdapter(ComparisonRequest).validate_json(raw, strict=True)


def build_operating_point(snapshot: FixedStateSnapshot, model: NetworkModelV2) -> NetworkOperatingPoint:
    """Every bus requires a gross load row, including explicit zero-load buses."""
    bus_ids = {b.bus_id for b in model.buses}
    if {load.bus_id for load in snapshot.loads} != bus_ids:
        raise ValueError("LOAD_BUS_MAPPING_MISMATCH: provide one gross load row per network bus")
    p = {load.bus_id: load.p_mw for load in snapshot.loads}
    q = {load.bus_id: load.q_mvar for load in snapshot.loads}
    for device in snapshot.devices:
        if device.bus_id not in bus_ids:
            raise ValueError(f"DEVICE_BUS_UNKNOWN:{device.resource_id}:{device.bus_id}")
        p[device.bus_id] -= device.p_mw
        q[device.bus_id] -= device.q_mvar
    for bus_id in bus_ids:
        _number(p[bus_id], f"net P at {bus_id}")
        _number(q[bus_id], f"net Q at {bus_id}")
    return NetworkOperatingPoint(snapshot.snapshot_id, snapshot.source, p, q,
                                 snapshot.quality_valid, snapshot.coherent)


def perturb_snapshot(snapshot: FixedStateSnapshot, change: DevicePerturbation) -> FixedStateSnapshot:
    matches = [d for d in snapshot.devices if d.resource_id == change.resource_id]
    if len(matches) != 1:
        raise ValueError(f"DEVICE_NOT_FOUND:{change.resource_id}")
    target = matches[0]
    if not target.in_service:
        raise ValueError(f"DEVICE_OUT_OF_SERVICE:{change.resource_id}")
    changed = replace(target, **{change.quantity: getattr(target, change.quantity) + change.delta})
    return replace(snapshot, snapshot_id=snapshot.snapshot_id + ":perturbed",
                   source=f"counterfactual perturbation of {snapshot.snapshot_id}; baseline source: {snapshot.source}",
                   devices=tuple(changed if d.resource_id == target.resource_id else d for d in snapshot.devices))


def device_capability_violations(snapshot: FixedStateSnapshot, tolerance: float) -> list[dict]:
    """Report declared device limits without clipping fixed/executed injections."""
    rows = []
    for d in snapshot.devices:
        for name, actual, bound, lower in (
            ("P_MIN", d.p_mw, d.p_min_mw, True), ("P_MAX", d.p_mw, d.p_max_mw, False),
            ("Q_MIN", d.q_mvar, d.q_min_mvar, True), ("Q_MAX", d.q_mvar, d.q_max_mvar, False),
            ("S_MAX", hypot(d.p_mw, d.q_mvar), d.s_max_mva, False),
        ):
            if bound is not None and (actual < bound - tolerance if lower else actual > bound + tolerance):
                rows.append(dict(resource_id=d.resource_id, code=name, actual=actual, limit=bound))
    return rows


# Compatibility for existing callers; one evaluator owns the bound checks.
_capability_violations = device_capability_violations


def _difference_rows(before, after, identity: str, quantities: tuple[str, ...]) -> list[dict]:
    lookup = {getattr(item, identity): item for item in after}
    rows = []
    for item in before:
        other = lookup[getattr(item, identity)]
        row = {identity: getattr(item, identity)}
        if hasattr(item, "parent_bus_id"):
            row.update(parent_bus_id=item.parent_bus_id, child_bus_id=item.child_bus_id, kind=item.kind)
        for key in quantities:
            first, second = getattr(item, key), getattr(other, key)
            row.update({f"before_{key}": first, f"after_{key}": second,
                        f"delta_{key}": second - first if first is not None and second is not None else None})
        rows.append(row)
    return rows


def compare_fixed_states(request: ComparisonRequest) -> dict:
    """Return evidence even for violations; differences only exist for two converged solves."""
    snapshot, model = request.snapshot, request.network
    report = dict(scope="single_device_fixed_state_ac_comparison", field_acceptance_certified=False,
                  network_id=model.network_id, dataset_version=model.provenance.dataset_version,
                  network_source=model.provenance.source, synthetic=model.provenance.synthetic,
                  operating_mode_id=request.operating_mode_id, baseline_snapshot_id=snapshot.snapshot_id,
                  snapshot_at=snapshot.at, snapshot_source=snapshot.source,
                  perturbation=asdict(request.perturbation),
                  redispatch_performed=False, status="invalid_input", reasons=[],
                  changed_snapshot=None, after_state_kind="counterfactual",
                  operating_points=[], before=None, after=None,
                  differences=None, device_violations={},
                  held_quantity="q_mvar" if request.perturbation.quantity == "p_mw" else "p_mw",
                  load_basis=snapshot.load_basis, slack_voltage_pu=snapshot.slack_voltage_pu,
                  capability_bounds_complete=all(
                      all(getattr(d, key) is not None for key in
                          ("p_min_mw", "p_max_mw", "q_min_mvar", "q_max_mvar", "s_max_mva"))
                      for d in snapshot.devices))
    readiness = assess_network_model(model, request.operating_mode_id,
                                     require_field_approval=request.require_field_approval)
    report["network_readiness"] = dict(blockers=list(readiness.blockers),
                                      warnings=list(readiness.warnings),
                                      disconnected_bus_ids=list(readiness.disconnected_bus_ids))
    if not readiness.structurally_valid:
        report["reasons"] = list(readiness.blockers)
        return report
    if readiness.disconnected_bus_ids:
        report.update(status="islanded", reasons=list(readiness.blockers))
        return report
    if not readiness.ready_for_current_solver:
        report.update(status="unsupported", reasons=list(readiness.blockers))
        return report
    if request.require_field_approval and not readiness.ready_for_field_case:
        report["reasons"] = list(readiness.blockers)
        return report
    missing_kv = [b.bus_id for b in model.buses if b.nominal_voltage_kv is None]
    if missing_kv:
        report["reasons"] = ["MISSING_NOMINAL_VOLTAGE:" + b for b in missing_kv]
        return report
    try:
        before_point = build_operating_point(snapshot, model)
        changed = perturb_snapshot(snapshot, request.perturbation)
        after_point = build_operating_point(changed, model)
    except ValueError as exc:
        report["reasons"] = [str(exc)]
        return report
    report["changed_snapshot"] = asdict(changed)
    report["operating_points"] = [dict(point_id=p.point_id, source=p.source,
                                        quality_valid=p.quality_valid, coherent=p.coherent,
                                        p_demand_mw_by_bus=dict(p.p_demand_mw_by_bus),
                                        q_demand_mvar_by_bus=dict(p.q_demand_mvar_by_bus))
                                  for p in (before_point, after_point)]
    network = readiness.require_current_solver_ready()
    before, after = (evaluate_operating_point(network, p, request.limits,
                                             slack_voltage_pu=snapshot.slack_voltage_pu)
                     for p in (before_point, after_point))
    report.update(before=asdict(before), after=asdict(after))
    report["device_violations"] = {
        "before": _capability_violations(snapshot, request.limits.comparison_tolerance),
        "after": _capability_violations(changed, request.limits.comparison_tolerance),
    }
    for failure in (ScenarioStatus.INVALID_INPUT, ScenarioStatus.NOT_CONVERGED):
        if before.status is failure or after.status is failure:
            report.update(status=failure.value, reasons=list(before.reasons + after.reasons))
            return report
    violated = (before.status is ScenarioStatus.VIOLATION or after.status is ScenarioStatus.VIOLATION
                or any(report["device_violations"].values()))
    report["status"] = "violation" if violated else "secure"
    report["differences"] = dict(
        buses=_difference_rows(before.buses, after.buses, "bus_id",
                               ("voltage_pu", "voltage_kv", "fixed_shunt_q_mvar")),
        branches=_difference_rows(before.branches, after.branches, "branch_id",
                                  ("sending_p_mw", "sending_q_mvar", "receiving_p_mw", "receiving_q_mvar",
                                   "sending_current_a", "receiving_current_a", "loading_pu",
                                   "active_loss_mw", "reactive_loss_mvar")),
        system=[dict(quantity=key, before=getattr(before, key), after=getattr(after, key),
                     delta=getattr(after, key) - getattr(before, key)) for key in
                ("pcc_import_mw", "pcc_reactive_mvar", "power_factor", "loss_mw", "reactive_loss_mvar")],
    )
    def direction(result: PointFlowResult) -> str:
        p = result.pcc_import_mw
        eps = request.limits.comparison_tolerance
        return "import" if p > eps else "export" if p < -eps else "zero_exchange"
    report["pcc_direction"] = dict(before=direction(before), after=direction(after))
    return report


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot serialize {type(value)}")


def write_comparison_outputs(output: Path, request: ComparisonRequest, report: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in (("request.json", asdict(request)), ("comparison.json", report)):
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                              allow_nan=False, default=_json_default), encoding="utf-8")
    # Always rewrite CSVs, including failed reruns, so stale success rows cannot survive.
    for name, fallback in (("buses", "bus_id"), ("branches", "branch_id"), ("system", "quantity")):
        rows = (report.get("differences") or {}).get(name, [])
        with (output / f"{name}_comparison.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else [fallback])
            writer.writeheader()
            writer.writerows(rows)


def run_comparison_file(input_path: Path, output: Path) -> int:
    """CLI exit 0 includes converged violations; failures have nonzero status."""
    request = read_comparison_request(input_path)
    report = compare_fixed_states(request)
    write_comparison_outputs(output, request, report)
    print(f"Fixed-state comparison: {report['status']}; redispatch=False")
    print(f"Results written to: {output.resolve()}")
    return 0 if report["status"] in ("secure", "violation") else 2

"""Legacy case boundary: explicit JSON profiles, network ledger and device mapping.

Any is confined to translating legacy numerical dataclasses, never HTTP contracts.
"""
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, cast

import numpy as np
from pydantic import TypeAdapter

from oilfield_energy.data import (
    Line,
    MicrogridData,
    ModelAssumptions,
    ProjectCase,
    Storage,
)
from oilfield_energy.minute_network import MinuteNetworkEvaluator
from oilfield_energy.modules.control.contracts import PlantInputs
from oilfield_energy.modules.demo_simulation.contracts import (
    Bundle,
    DeviceSpec,
    Fault,
    Reading,
    Safety,
)
from oilfield_energy.modules.resources.contracts import ResourceIdentity, ResourceKind
from oilfield_energy.modules.studies.api import generate_inputs
from oilfield_energy.network_model import (
    NetworkContingency,
    NetworkModelV2,
    validate_legacy_network_alignment,
)
from oilfield_energy.network_scenarios import (
    NetworkOperatingPoint,
    NetworkSecurityLimits,
    build_network_scenarios,
    evaluate_network_scenarios,
)
from oilfield_energy.power_flow_comparison import BusLoadState, DeviceState, FixedStateSnapshot
from oilfield_energy.resource_control_contracts import ResourceType

from .project_dataset import build_synthetic_case, study_recipe


def plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def encode(value: Any) -> bytes:
    return json.dumps(plain(value), ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_bundle(path: Path) -> tuple[Bundle, bytes]:
    raw = path.read_bytes()
    digest = path.with_suffix(".sha256").read_text(encoding="ascii").strip()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("demo bundle SHA-256 mismatch")
    return decode_bundle(raw), raw


def decode_bundle(raw: bytes) -> Bundle:
    json.loads(raw, object_pairs_hook=reject_duplicates)
    bundle = Bundle.model_validate_json(raw)
    case = case_from_bundle(bundle, 96)
    if case.dataset_id is not None and bundle.dataset_id != case.dataset_id:
        raise ValueError("bundle and case identity differ")
    if bundle.plant_profiles is not None:
        if set(bundle.plant_profiles) != {m.name for m in case.microgrids}:
            raise ValueError("plant region coverage mismatch")
        for mg in case.microgrids:
            profile = PlantInputs.model_validate_json(
                encode(bundle.plant_profiles[mg.name]), strict=True
            )
            if profile.start != bundle.start or profile.steps != 1440 or profile.step_minutes != 1:
                raise ValueError("plant time axis mismatch")
            if {s.bus_id for s in profile.load_p} != set(mg.buses) or {
                s.bus_id for s in profile.load_q
            } != set(mg.buses):
                raise ValueError("plant load coverage mismatch")
            for series, capacities in (
                (profile.wind_available, mg.wind_capacity_mw),
                (profile.pv_available, mg.pv_capacity_mw),
            ):
                if {s.bus_id for s in series} != set(capacities) or any(
                    v < 0 or v > capacities[s.bus_id] for s in series for v in s.values
                ):
                    raise ValueError("plant renewable coverage or capacity mismatch")
    device_pairs = {(d.region, d.bus_id) for d in bundle.devices}
    if any(d.region not in {m.name for m in case.microgrids} for d in bundle.devices):
        raise ValueError("unknown device region")
    for mg in case.microgrids:
        expected = set(mg.wind_capacity_mw) | set(mg.pv_capacity_mw) | {mg.storage.bus, mg.svg_bus}
        if {bus for region, bus in device_pairs if region == mg.name} != expected:
            raise ValueError("device mapping does not match case resources")
        for kind, capacities in (("wind", mg.wind_capacity_mw), ("pv", mg.pv_capacity_mw)):
            for bus, capacity in capacities.items():
                total = sum(
                    d.p_max_mw
                    for d in bundle.devices
                    if d.region == mg.name and d.bus_id == bus and d.kind == kind
                )
                if abs(total - capacity) > 1e-8:
                    raise ValueError("device capacity does not match case")
        for kind, bus in (("storage", mg.storage.bus), ("svg", mg.svg_bus)):
            matching = [d for d in bundle.devices if d.region == mg.name and d.kind == kind]
            if len(matching) != 1 or matching[0].bus_id != bus:
                raise ValueError("storage/SVG mapping must be one-to-one")
            spec = matching[0]
            if kind == "storage":
                expected_values = (
                    mg.storage.p_max_mw,
                    mg.storage.e_max_mwh,
                    mg.storage.e_min_mwh,
                    mg.storage.e_initial_mwh,
                    mg.storage.eta_charge,
                    mg.storage.eta_discharge,
                    mg.storage.s_max_mva,
                )
                actual_values = (
                    spec.p_max_mw,
                    spec.energy_mwh,
                    spec.minimum_mwh,
                    spec.initial_mwh,
                    spec.eta_charge,
                    spec.eta_discharge,
                    spec.s_max_mva,
                )
                if actual_values != expected_values or spec.p_min_mw != -spec.p_max_mw:
                    raise ValueError("storage parameters do not match case")
            elif spec.q_max_mvar != mg.svg_q_max_mvar or -spec.q_max_mvar != mg.svg_q_min_mvar:
                raise ValueError("SVG parameters do not match case")
    return bundle


def case_from_bundle(bundle: Bundle, steps: int) -> ProjectCase:
    if not 1 <= steps <= 288:
        raise ValueError("steps must be in 1..288")
    data: dict[str, Any] = cast(dict[str, Any], bundle.case)
    identity_fields = {"dataset_id", "dataset_revision", "dataset_sha256", "profile_kind"}
    if set(data) - identity_fields != {f.name for f in fields(ProjectCase)} - identity_fields:
        raise ValueError("unexpected case fields")
    original = np.array(data["time_hours"], dtype=float)
    if len(original) != 96 or not np.allclose(original, np.arange(96) / 4):
        raise ValueError("bundle requires 96 explicit quarter-hour samples")
    hours = np.arange(steps) * (24.0 / steps)

    def profile(value: Any, *, nonnegative: bool = True) -> np.ndarray:
        if not isinstance(value, list) or any(type(v) not in (int, float) for v in value):
            raise ValueError(
                "profile samples must be explicit numeric values, not strings or booleans"
            )
        arr = np.asarray(value, dtype=float)
        if arr.shape != (96,) or not np.isfinite(arr).all() or (nonnegative and (arr < 0).any()):
            raise ValueError("invalid profile shape, quality or sign")
        return np.interp(hours, original, arr, period=24.0)

    grids: list[MicrogridData] = []
    for source in data["microgrids"]:
        item = dict(source)
        for key in ("load_p_mw", "load_q_mvar", "wind_available_mw", "pv_available_mw"):
            item[key] = {
                bus: profile(v, nonnegative=key != "load_q_mvar") for bus, v in item[key].items()
            }
        item["lines"] = [Line(**line) for line in item["lines"]]
        item["storage"] = Storage(**item["storage"])
        item["resource_identities"] = tuple(
            ResourceIdentity(**identity) for identity in item.get("resource_identities", [])
        )
        item["network_model_v2"] = TypeAdapter(NetworkModelV2).validate_json(
            encode(item["network_model_v2"]), strict=True
        )
        mg = MicrogridData(**item)
        if (
            not 0 < mg.voltage_min_pu < mg.voltage_max_pu
            or not 0 <= mg.p_grid_min_mw < mg.p_grid_max_mw
        ):
            raise ValueError("invalid voltage/PCC limits")
        validate_legacy_network_alignment(mg)
        if set(mg.load_p_mw) != set(mg.buses) or set(mg.load_q_mvar) != set(mg.buses):
            raise ValueError("gross P/Q loads must cover every bus")
        for values, caps in (
            (mg.wind_available_mw, mg.wind_capacity_mw),
            (mg.pv_available_mw, mg.pv_capacity_mw),
        ):
            if set(values) != set(caps) or not set(caps) <= set(mg.buses):
                raise ValueError("resource profile mapping mismatch")
            if any(
                type(cap) not in (float, int) or not np.isfinite(cap) or cap <= 0
                for cap in caps.values()
            ):
                raise ValueError("resource ratings must be finite and positive")
            if any((values[bus] > cap + 1e-8).any() for bus, cap in caps.items()):
                raise ValueError("availability exceeds rated active power")
        grids.append(mg)
    if {m.name for m in grids} != {"SC", "YA_B", "YA_C"} or len(grids) != 3:
        raise ValueError("exactly three unique demo regions are required")
    return ProjectCase(
        hours,
        np.asarray(data["price_cny_per_mwh"], dtype=float)[np.minimum((hours * 4).astype(int), 95)]
        if data.get("dataset_id")
        else profile(data["price_cny_per_mwh"]),
        grids,
        replace(ModelAssumptions(**data["assumptions"]), dt_hours=24.0 / steps),
        float(data["cluster_import_limit_mw"]),
        **{key: data.get(key) for key in identity_fields},
    )


def build_bundle() -> Bundle:
    case = build_synthetic_case(96)
    # Give all synthetic buses explicit voltage bases so current is calculable.
    grids: list[MicrogridData] = []
    devices: list[DeviceSpec] = []
    for mg in case.microgrids:
        assert mg.network_model_v2 is not None
        network = replace(
            mg.network_model_v2,
            contingencies=tuple(
                NetworkContingency(
                    f"N-1:{b.branch_id}",
                    (b.branch_id,),
                    provenance="synthetic explicit single-branch outage",
                )
                for b in mg.network_model_v2.branches
            ),
        )
        mg = replace(mg, network_model_v2=network)
        grids.append(mg)
        for kind, caps, apparent in (
            ("wind", mg.wind_capacity_mw, mg.wind_capacity_mva),
            ("pv", mg.pv_capacity_mw, mg.pv_capacity_mva),
        ):
            for bus, cap in caps.items():
                count = 1
                for _ in range(count):
                    devices.append(
                        DeviceSpec.model_validate(
                            dict(
                                device_id=mg.resource_id(cast(ResourceKind, kind), bus),
                                region=mg.name,
                                bus_id=bus,
                                kind=kind,
                                p_min_mw=0.0,
                                p_max_mw=cap / count,
                                q_max_mvar=next(
                                    r.q_max_mvar
                                    for r in study_recipe(mg.name).resources
                                    if r.bus_id == bus and r.kind == kind
                                )
                                if kind == "wind"
                                else 0.0,
                                s_max_mva=apparent[bus] / count,
                                ramp_mw_per_minute=1.0,
                                q_abs_over_p_max=mg.wind_q_over_p_limit(bus)
                                if kind == "wind"
                                else None,
                            )
                        )
                    )
        st = mg.storage
        devices.append(
            DeviceSpec(
                device_id=mg.resource_id("storage", st.bus),
                region=mg.name,
                bus_id=st.bus,
                kind="storage",
                p_min_mw=-st.p_max_mw,
                p_max_mw=st.p_max_mw,
                q_max_mvar=0.0,
                s_max_mva=st.s_max_mva,
                ramp_mw_per_minute=1.0,
                energy_mwh=st.e_max_mwh,
                minimum_mwh=st.e_min_mwh,
                initial_mwh=st.e_initial_mwh,
                eta_charge=st.eta_charge,
                eta_discharge=st.eta_discharge,
            )
        )
        devices.append(
            DeviceSpec(
                device_id=mg.resource_id("svg", mg.svg_bus),
                region=mg.name,
                bus_id=mg.svg_bus,
                kind="svg",
                p_min_mw=0.0,
                p_max_mw=0.0,
                q_max_mvar=mg.svg_q_max_mvar,
                s_max_mva=mg.svg_q_max_mvar,
                ramp_mw_per_minute=1.0,
            )
        )
    bundle = Bundle(
        schema_version="oilfield-demo-v1",
        dataset_id=case.dataset_id or "unknown",
        synthetic=True,
        start=study_recipe().start,
        source="Deterministic synthetic engineering assumptions; no client files",
        case=plain(replace(case, microgrids=grids)),
        devices=tuple(devices),
        plant_profiles={
            name: json.loads(generate_inputs(study_recipe(name))[2].model_dump_json())
            for name in ("SC", "YA_B", "YA_C")
        },
        presets={
            name: {"name": f"模拟演示：{name}", "scenario_type": name, "steps": 8}
            for name in (
                "single_microgrid",
                "cluster_coordination",
                "communication_fault",
                "group_control",
            )
        },
    )
    raw = bundle.model_dump_json(indent=2).encode("utf-8")
    decode_bundle(raw)
    return bundle


def generate_bundle(output: Path) -> Bundle:
    bundle = build_bundle()
    raw = bundle.model_dump_json(indent=2).encode("utf-8")
    output.mkdir(parents=True, exist_ok=False)
    (output / "bundle.json").write_bytes(raw)
    (output / "bundle.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    (output / "README.md").write_text(
        "# 合成演示资料\n\n全部为模拟值，未读取甲方文件。\n"
        "由安装包 unified_dataset.json 生成，禁止手工维护第二份台账。\n"
        "山城七母线，两台 5 MW 风机独立接入；YA_B、YA_C 各五母线。\n"
        "96 点日前预测覆盖完整一天；正在运行的服务和作业仍使用已捕获的版本。\n",
        encoding="utf-8",
    )
    return bundle


class DemoNetwork:
    def __init__(self, bundle: Bundle):
        self.bundle = bundle
        self.case = case_from_bundle(bundle, 96)
        self.plant = {
            region: PlantInputs.model_validate_json(encode(profile), strict=True)
            for region, profile in (bundle.plant_profiles or {}).items()
        }

    def screen_contingencies(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for mg in self.case.microgrids:
            assert mg.network_model_v2 is not None
            point = NetworkOperatingPoint(
                "fixed-midnight",
                self.bundle.dataset_id,
                {
                    bus: float(mg.load_p_mw[bus][0])
                    - sum(
                        float(v[0])
                        for mapping in (mg.wind_available_mw, mg.pv_available_mw)
                        for key, v in mapping.items()
                        if key == bus
                    )
                    for bus in mg.buses
                },
                {bus: float(mg.load_q_mvar[bus][0]) for bus in mg.buses},
                True,
                True,
            )
            batch = evaluate_network_scenarios(
                mg.network_model_v2,
                (point,),
                build_network_scenarios(mg.network_model_v2),
                NetworkSecurityLimits(
                    mg.voltage_min_pu, mg.voltage_max_pu, mg.p_grid_min_mw, mg.p_grid_max_mw, 0.9
                ),
            )
            results[mg.name] = plain(batch)
        return results

    def available(self, device: DeviceSpec, minute: int) -> float:
        mg = next(m for m in self.case.microgrids if m.name == device.region)
        profiles = mg.wind_available_mw if device.kind == "wind" else mg.pv_available_mw
        if device.kind not in {"wind", "pv"}:
            return device.p_max_mw
        if device.region in self.plant:
            plant = self.plant[device.region]
            series = plant.wind_available if device.kind == "wind" else plant.pv_available
            return next(s.values[minute % plant.steps] for s in series if s.bus_id == device.bus_id)
        capacities = mg.wind_capacity_mw if device.kind == "wind" else mg.pv_capacity_mw
        index = (minute % 1440) // 15
        return float(profiles[device.bus_id][index]) * device.p_max_mw / capacities[device.bus_id]

    def evaluate(
        self,
        readings: tuple[Reading, ...],
        minute: int,
        at: datetime,
        observed: datetime,
        fault: Fault,
    ) -> tuple[Safety, ...]:
        results: list[Safety] = []
        for mg in self.case.microgrids:
            rows = [r for r in readings if r.region == mg.name]
            limits = NetworkSecurityLimits(
                mg.voltage_min_pu,
                mg.voltage_max_pu,
                mg.p_grid_min_mw,
                mg.p_grid_max_mw,
                0.9,
                max_iterations=1 if fault == "nonconvergence" else 100,
            )
            index = (minute % 1440) // 15
            plant = self.plant.get(mg.name)

            def load(bus: str, reactive: bool = False) -> float:
                if plant is not None:
                    series = plant.load_q if reactive else plant.load_p
                    return next(s.values[minute % plant.steps] for s in series if s.bus_id == bus)
                return float((mg.load_q_mvar if reactive else mg.load_p_mw)[bus][index])

            snapshot = FixedStateSnapshot(
                f"demo:{minute}:{mg.name}",
                observed,
                self.bundle.dataset_id,
                "gross_bus_load",
                0.85 if fault == "voltage_sag" else 1.0,
                fault != "bad_quality",
                True,
                tuple(
                    BusLoadState(
                        bus,
                        load(bus) * (0.1 if fault == "load_drop" else 1),
                        load(bus, True) * (0.1 if fault == "load_drop" else 1),
                    )
                    for bus in mg.buses
                ),
                tuple(
                    DeviceState(r.device_id, r.bus_id, ResourceType(r.kind), r.p_mw, r.q_mvar, True)
                    for r in rows
                ),
            )
            feedback = MinuteNetworkEvaluator(
                mg.network_model_v2, limits, {r.device_id: r.bus_id for r in rows}
            ).evaluate(snapshot, at=at, operating_mode_id=mg.network_operating_mode_id or "normal")
            flow = feedback.flow
            pcc_safe = (
                flow is not None
                and flow.pcc_import_mw is not None
                and mg.p_grid_min_mw <= flow.pcc_import_mw <= mg.p_grid_max_mw
            )
            results.append(
                Safety(
                    region=mg.name,
                    status=feedback.status,
                    valid=feedback.valid,
                    recovery_safe=feedback.recovery_safe and pcc_safe,
                    reasons=feedback.reasons,
                    pcc_import_mw=flow.pcc_import_mw if flow else None,
                    pcc_q_mvar=flow.pcc_reactive_mvar if flow else None,
                    loss_mw=flow.loss_mw if flow else None,
                    power_factor=flow.power_factor if flow else None,
                    voltage_min_pu=min((b.voltage_pu for b in flow.buses), default=None)
                    if flow
                    else None,
                    voltage_max_pu=max((b.voltage_pu for b in flow.buses), default=None)
                    if flow
                    else None,
                    max_loading_pu=max((b.loading_pu for b in flow.branches), default=None)
                    if flow
                    else None,
                    flow=plain(flow) if flow else None,
                )
            )
        return tuple(results)

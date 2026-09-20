"""Explicit file generation and loading for the synthetic study."""

import csv
import io
import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

from pydantic import JsonValue

from oilfield_energy.modules.control.contracts import PlantInputs
from oilfield_energy.modules.measurements.contracts import CapturedDataset
from oilfield_energy.modules.studies.api import generate_inputs
from oilfield_energy.modules.studies.contracts import StudySpec
from oilfield_energy.workflows.field_dataset.contracts import Artifact, ResultStore


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()


def validate_json(raw: bytes) -> bytes:
    def unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate simulation JSON key: {key}")
            result[key] = value
        return result

    def invalid(value: str) -> JsonValue:
        raise ValueError(f"nonfinite simulation JSON number: {value}")

    json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    return raw


def generate_dataset(config: Path | StudySpec, output: Path, store: ResultStore) -> None:
    raw = (
        config.model_dump_json(indent=2).encode()
        if isinstance(config, StudySpec)
        else validate_json(config.read_bytes())
    )
    spec = StudySpec.model_validate_json(raw, strict=True)
    day, intraday, plant = generate_inputs(spec)
    documents: dict[str, object] = {}
    devices = [
        dict(
            resource_id=r.resource_id,
            bus_id=r.bus_id,
            resource_type=r.kind,
            p_min_mw=-r.p_max_mw if r.kind == "storage" else 0.0,
            p_max_mw=r.p_max_mw,
            q_min_mvar=-r.q_max_mvar,
            q_max_mvar=r.q_max_mvar,
            s_max_mva=r.s_max_mva,
        )
        for r in spec.resources
    ]
    documents["network"] = spec.network
    documents["devices"] = dict(
        devices=devices,
        loads=[
            dict(
                load_id="load:" + load.bus_id,
                bus_id=load.bus_id,
                basis="gross_load",
                scope_members=[load.bus_id],
            )
            for load in spec.loads
        ],
        zero_load_bus_ids=[
            b for b in spec.bus_ids if b not in {load.bus_id for load in spec.loads}
        ],
    )
    documents["operating_modes"] = dict(
        network_id=spec.network["network_id"],
        default_operating_mode_id="normal",
        operating_modes=[dict(mode_id="normal", description="synthetic normal mode")],
        selections=[
            dict(
                mode_id="normal",
                source="synthetic fixed topology",
                valid_from=spec.start.isoformat(),
                valid_until=(spec.start + timedelta(minutes=spec.intervals * 15)).isoformat(),
            )
        ],
    )
    documents["settings"] = dict(
        limits=dict(
            voltage_min_pu=spec.voltage_min_pu,
            voltage_max_pu=spec.voltage_max_pu,
            pcc_import_min_mw=spec.pcc_min_mw,
            pcc_import_max_mw=spec.pcc_max_mw,
            power_factor_min=spec.pf_min,
        ),
        max_age_seconds=60.0,
        max_skew_seconds=5.0,
        inventory_complete=True,
        mapping_confirmed=True,
        metering_scopes_confirmed=True,
    )
    points: list[dict[str, object]] = []
    series: dict[str, tuple[float, ...]] = {}

    def point(
        target: str, kind: str, quantity: str, unit: str, direction: str, values: tuple[float, ...]
    ) -> None:
        point_id = target + "." + quantity
        points.append(
            dict(
                point_id=point_id,
                source_id="telemetry",
                target_kind=kind,
                target_id=target,
                quantity=quantity,
                unit=unit,
                multiplier=1.0,
                positive_direction=direction,
                sample_kind="instantaneous",
            )
        )
        series[point_id] = values

    available = {s.bus_id: s.values for s in (*plant.wind_available, *plant.pv_available)}
    for r in spec.resources:
        p = available[r.bus_id] if r.kind in ("wind", "pv") else (0.0,) * plant.steps
        q = (min(2.0, r.q_max_mvar),) * plant.steps if r.kind == "svg" else (0.0,) * plant.steps
        point(r.resource_id, "device", "p", "MW", "injection", p)
        point(r.resource_id, "device", "q", "Mvar", "injection", q)
        point(r.resource_id, "device", "in_service", "bool", "on", (1.0,) * plant.steps)
    for load in spec.loads:
        for quantity, group, unit in (("p", plant.load_p, "MW"), ("q", plant.load_q, "Mvar")):
            point(
                "load:" + load.bus_id,
                "load",
                quantity,
                unit,
                "consumption",
                next(s.values for s in group if s.bus_id == load.bus_id),
            )
    point(
        str(spec.network["pcc_bus_id"]),
        "boundary",
        "voltage",
        "pu",
        "magnitude",
        (1.0,) * plant.steps,
    )
    offset = spec.start.utcoffset()
    if offset is None:
        raise ValueError("simulation timezone missing")
    documents["point_mapping"] = dict(
        sources=[
            dict(
                source_id="telemetry",
                file_id="telemetry",
                format="csv",
                point_column="point",
                time_column="time",
                value_column="value",
                quality_column="quality",
                good_quality="GOOD",
                timestamp_format="iso8601",
                utc_offset_minutes=int(offset.total_seconds() / 60),
            )
        ],
        points=points,
    )
    artifacts: list[Artifact] = []
    refs: list[dict[str, object]] = []

    def add(file_id: str, role: str, path: str, content: bytes) -> None:
        artifacts.append(Artifact(path, content))
        refs.append(
            dict(
                file_id=file_id,
                role=role,
                path=path,
                version=spec.revision,
                sha256=sha256(content).hexdigest(),
            )
        )

    for role, payload in documents.items():
        add(
            role,
            role,
            role + ".json",
            json_bytes(
                dict(
                    schema_version="field-document-v1",
                    dataset_id=spec.dataset_id,
                    version=spec.revision,
                    synthetic=True,
                    role=role,
                    payload=payload,
                )
            ),
        )
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(("point", "time", "value", "quality"))
    for i in range(plant.steps):
        at = (spec.start + timedelta(minutes=i)).isoformat()
        for point_id, values in series.items():
            writer.writerow((point_id, at, values[i], "GOOD"))
    add("telemetry", "measurements", "measurements.csv", stream.getvalue().encode())
    add("study_recipe", "attachment", "study_recipe.json", raw)
    for name, values in (("day_ahead", day), ("intraday", intraday), ("plant", plant)):
        add(name, "attachment", name + ".json", values.model_dump_json(indent=2).encode())
    add(
        "scenario_catalog",
        "attachment",
        "scenarios.json",
        json_bytes(
            dict(
                schema_version="SC-scenarios-v1",
                seed=spec.seed,
                cases=[
                    "normal",
                    "load_drop",
                    "wind_trip",
                    "bad_quality",
                    "stale_telemetry",
                    "voltage_violation",
                    "nonconvergence",
                ],
                fault_start_minute=10,
                fault_end_minute=15,
                rearm_minute=20,
                window_intervals=4,
            )
        ),
    )
    artifacts.append(
        Artifact(
            "manifest.json",
            json_bytes(
                dict(
                    schema_version="field-dataset-v1",
                    dataset_id=spec.dataset_id,
                    revision=spec.revision,
                    source="generated synthetic study; no field approval",
                    synthetic=True,
                    files=refs,
                )
            ),
        )
    )
    store.publish(output, tuple(artifacts))


def captured_content(dataset: CapturedDataset, file_id: str) -> bytes:
    matches = [f.content for f in dataset.files if f.reference.file_id == file_id]
    if len(matches) != 1:
        raise ValueError(f"required study file missing: {file_id}")
    return validate_json(matches[0])


def load_study(dataset: CapturedDataset) -> tuple[StudySpec, PlantInputs, PlantInputs, PlantInputs]:
    spec = StudySpec.model_validate_json(captured_content(dataset, "study_recipe"), strict=True)
    if (
        not dataset.manifest.synthetic
        or spec.dataset_id != dataset.manifest.dataset_id
        or spec.revision != dataset.manifest.revision
    ):
        raise ValueError("study identity mismatch or not synthetic")
    network: dict[str, JsonValue] = json.loads(
        next(f.content for f in dataset.files if f.reference.role == "network")
    )
    if network["payload"] != spec.network:
        raise ValueError("recipe and network document differ")
    profiles = tuple(
        PlantInputs.model_validate_json(captured_content(dataset, name), strict=True)
        for name in ("day_ahead", "intraday", "plant")
    )
    for i, profile in enumerate(profiles):
        step = 1 if i == 2 else 15
        if (
            profile.start != spec.start
            or profile.step_minutes != step
            or profile.steps * step != spec.intervals * 15
        ):
            raise ValueError("study profile time alignment mismatch")
        if {s.bus_id for s in profile.load_p} != set(spec.bus_ids):
            raise ValueError("study load coverage mismatch")
        for kind, series in (("wind", profile.wind_available), ("pv", profile.pv_available)):
            capacities = {r.bus_id: r.p_max_mw for r in spec.resources if r.kind == kind}
            if {s.bus_id for s in series} != set(capacities) or any(
                v > capacities[s.bus_id] for s in series for v in s.values
            ):
                raise ValueError("study renewable coverage or capacity mismatch")
    return spec, profiles[0], profiles[1], profiles[2]

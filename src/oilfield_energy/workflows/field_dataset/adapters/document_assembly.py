"""Decode split boundary documents into the existing fixed-snapshot wire format.

No physical formulas live here. Unknown payload keys are rejected before mapping;
the receiving engine validates all nested network/device/measurement structures.
"""

import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue

from oilfield_energy.modules.measurements.contracts import CapturedDataset, DatasetError, FileRole

from ..api import archive_path


class DocumentEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["field-document-v1"]
    dataset_id: str
    version: str
    synthetic: bool
    role: FileRole
    payload: dict[str, JsonValue]


class ModeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode_id: str
    source: str
    valid_from: AwareDatetime
    valid_until: AwareDatetime


def _unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError(f"DUPLICATE_JSON_KEY:{key}")
        result[key] = value
    return result


def _invalid_number(value: str) -> JsonValue:
    raise DatasetError(f"NONFINITE_JSON_NUMBER:{value}")


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise DatasetError("OBJECT_REQUIRED")
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise DatasetError("ARRAY_REQUIRED")
    return value


def _keys(payload: dict[str, JsonValue], expected: set[str], role: str) -> None:
    if set(payload) != expected:
        raise DatasetError(f"DOCUMENT_FIELDS_INVALID:{role}:expected={sorted(expected)}")


def assemble_request(dataset: CapturedDataset, at: datetime, mode: str | None) -> bytes:
    documents: dict[str, dict[str, JsonValue]] = {}
    for item in dataset.files:
        ref = item.reference
        if ref.role in ("measurements", "attachment"):
            continue
        text = item.content.decode("utf-8-sig")
        json.loads(text, object_pairs_hook=_unique, parse_constant=_invalid_number)
        document = DocumentEnvelope.model_validate_json(text, strict=True)
        if (
            document.dataset_id != dataset.manifest.dataset_id
            or document.version != ref.version
            or document.synthetic != dataset.manifest.synthetic
            or document.role != ref.role
        ):
            raise DatasetError(f"DOCUMENT_IDENTITY_MISMATCH:{ref.file_id}")
        documents[ref.role] = document.payload
    network = documents["network"]
    if "operating_modes" in network or "default_operating_mode_id" in network:
        raise DatasetError("OPERATING_MODES_MUST_HAVE_ONE_SOURCE")
    devices, mapping, settings, modes = (
        documents[key] for key in ("devices", "point_mapping", "settings", "operating_modes")
    )
    _keys(devices, {"devices", "loads", "zero_load_bus_ids"}, "devices")
    _keys(mapping, {"sources", "points"}, "point_mapping")
    _keys(
        settings,
        {
            "limits",
            "max_age_seconds",
            "max_skew_seconds",
            "inventory_complete",
            "mapping_confirmed",
            "metering_scopes_confirmed",
        },
        "settings",
    )
    _keys(
        modes,
        {"network_id", "default_operating_mode_id", "operating_modes", "selections"},
        "operating_modes",
    )
    if modes["network_id"] != network.get("network_id"):
        raise DatasetError("OPERATING_MODE_NETWORK_MISMATCH")
    selected_id = mode if mode is not None else modes["default_operating_mode_id"]
    selections = [
        ModeSelection.model_validate_json(json.dumps(value), strict=True)
        for value in _array(modes["selections"])
    ]
    known_mode_ids = [_object(value).get("mode_id") for value in _array(modes["operating_modes"])]
    if any(value.mode_id not in known_mode_ids for value in selections):
        raise DatasetError("OPERATING_MODE_SELECTION_UNKNOWN")
    for index, value in enumerate(selections):
        if any(
            other.mode_id == value.mode_id
            and max(other.valid_from, value.valid_from) < min(other.valid_until, value.valid_until)
            for other in selections[index + 1 :]
        ):
            raise DatasetError("OPERATING_MODE_SELECTION_OVERLAP")
    if any(
        not value.source.strip() or value.valid_from >= value.valid_until for value in selections
    ):
        raise DatasetError("INVALID_MODE_VALIDITY_OR_SOURCE")
    matches = [
        value
        for value in selections
        if value.mode_id == selected_id and value.valid_from <= at < value.valid_until
    ]
    if len(matches) != 1:
        raise DatasetError("OPERATING_MODE_SELECTION_MISSING_OR_AMBIGUOUS")
    selection = matches[0]
    network = dict(
        network,
        operating_modes=modes["operating_modes"],
        default_operating_mode_id=modes["default_operating_mode_id"],
    )
    files = {item.reference.file_id: item.reference for item in dataset.files}
    sources: list[JsonValue] = []
    used: set[str] = set()
    for raw in _array(mapping["sources"]):
        source = dict(_object(raw))
        file_id = source.pop("file_id", None)
        if not isinstance(file_id, str) or file_id not in files or "path" in source:
            raise DatasetError("SOURCE_MUST_REFERENCE_MANIFEST_FILE_ID")
        reference = files[file_id]
        if reference.role != "measurements":
            raise DatasetError(f"SOURCE_FILE_ROLE_INVALID:{file_id}")
        source["path"] = f"dataset/{archive_path(file_id, reference.path)}"
        sources.append(source)
        used.add(file_id)
    if used != {ref.file_id for ref in files.values() if ref.role == "measurements"}:
        raise DatasetError("UNMAPPED_MEASUREMENT_FILE")
    request: dict[str, JsonValue] = dict(
        schema_version="field-snapshot-v1",
        dataset_id=dataset.manifest.dataset_id,
        source=dataset.manifest.source,
        synthetic=dataset.manifest.synthetic,
        network=network,
        at=at.isoformat(),
        operating_mode_id=selection.mode_id,
        operating_mode_source=selection.source,
        operating_mode_valid_from=selection.valid_from.isoformat(),
        operating_mode_valid_until=selection.valid_until.isoformat(),
        sources=sources,
        points=mapping["points"],
        **devices,
        **settings,
    )
    return json.dumps(request, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")

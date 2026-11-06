"""Shared device evidence exports. No cluster planning or adoption rules."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Mapping

from pydantic import TypeAdapter

from .hierarchy_types import DeviceTrackingResult
from .modules.control.contracts import (
    MinuteExecutionRecord,
    ResourcePowerSeries,
    StorageDynamicsTrajectory,
)


def write_minute_network_outputs(output: Path, tracking: Mapping[str, DeviceTrackingResult]) -> None:
    """Archive each causal phase, raw normalized injections, AC results and unknowns."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "storage_dynamics.json").write_text(
        TypeAdapter(dict[str, StorageDynamicsTrajectory])
        .dump_json(
            {
                name: StorageDynamicsTrajectory(
                    version="storage-dynamics-v1" if item.storage_dynamics else None,
                    complete=bool(item.storage_dynamics)
                    and len(item.storage_dynamics) == len(item.time_minutes),
                    records=item.storage_dynamics,
                )
                for name, item in tracking.items()
            },
            indent=2,
        )
        .decode("utf-8"),
        encoding="utf-8",
    )
    fields = ["microgrid", "time_minutes", "stage", "is_actual", "status", "valid", "reasons",
              "voltage_within_limits", "line_capacity_within_limits", "minimum_voltage_pu",
              "maximum_voltage_pu", "maximum_loading_pu", "pcc_import_mw", "pcc_reactive_mvar",
              "loss_mw", "recovery_blocked"]

    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(type(value).__name__)

    (output / "minute_network_context.json").write_text(json.dumps(
        {name: item.network_context for name, item in tracking.items()},
        ensure_ascii=False, indent=2, allow_nan=False, default=encode), encoding="utf-8")

    (output / "minute_execution.json").write_text(
        json.dumps(
            {
                name: asdict(
                    MinuteExecutionRecord(
                        version=item.execution_evidence_version,
                        time_minutes=tuple(float(v) for v in item.time_minutes),
                        wind_resource_actual_mw=tuple(
                            ResourcePowerSeries(rid, tuple(float(v) for v in values))
                            for rid, values in item.wind_resource_actual_mw.items()
                        ),
                        wind_availability_limited_mw=tuple(
                            float(v) for v in item.wind_availability_limited_mw
                        ),
                        pv_availability_limited_mw=tuple(
                            float(v) for v in item.pv_availability_limited_mw
                        ),
                        restoration_evidence=item.restoration_evidence,
                    )
                )
                for name, item in tracking.items()
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    with (output / "minute_network.jsonl").open("w", encoding="utf-8") as detail, \
         (output / "minute_network.csv").open("w", newline="", encoding="utf-8-sig") as table:
        writer = csv.DictWriter(table, fieldnames=fields)
        writer.writeheader()
        for name, item in tracking.items():
            blocked = dict(zip(item.time_minutes, item.network_recovery_blocked))
            for record in item.network_feedback:
                payload = dict(microgrid=name, **record)
                payload["recovery_blocked_at_end_of_step"] = bool(blocked[record["time_minutes"]])
                detail.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=encode) + "\n")
                flow = record["flow"] if record["valid"] else None
                row = {key: payload.get(key) for key in fields}
                row["recovery_blocked"] = payload["recovery_blocked_at_end_of_step"]
                row["reasons"] = ";".join(record["reasons"])
                if flow:
                    row.update(minimum_voltage_pu=min(b["voltage_pu"] for b in flow["buses"]),
                               maximum_voltage_pu=max(b["voltage_pu"] for b in flow["buses"]),
                               maximum_loading_pu=max((b["loading_pu"] for b in flow["branches"]), default=0.0),
                               pcc_import_mw=flow["pcc_import_mw"], pcc_reactive_mvar=flow["pcc_reactive_mvar"],
                               loss_mw=flow["loss_mw"])
                writer.writerow(row)

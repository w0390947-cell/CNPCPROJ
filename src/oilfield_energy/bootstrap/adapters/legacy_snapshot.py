"""ADR-0001: bounded bridge to the existing public fixed-state implementation.

Only captured bytes are materialized. Live source paths are never reopened here.
The temporary directory is a private input copy, not the result archive.
"""

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import TypeAdapter

from oilfield_energy.field_data.snapshot_power_flow import (
    build_field_snapshot,
    calculate_field_power_flow,
    read_snapshot_request,
    write_snapshot_outputs,
)
from oilfield_energy.modules.measurements.contracts import CapturedDataset
from oilfield_energy.power_flow_comparison import FixedStateSnapshot
from oilfield_energy.workflows.field_dataset.adapters.document_assembly import assemble_request
from oilfield_energy.workflows.field_dataset.api import dataset_artifacts
from oilfield_energy.workflows.field_dataset.contracts import Artifact, Evaluation


class LegacySnapshotEngine:
    def evaluate(
        self, dataset: CapturedDataset, *, at: datetime, mode: str | None, demo: bool, solve: bool
    ) -> Evaluation:
        request_bytes = assemble_request(dataset, at, mode)
        with TemporaryDirectory(prefix="field-dataset-") as directory:
            root = Path(directory)
            for item in dataset_artifacts(dataset):
                path = root / item.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item.content)
            request_path = root / "request.json"
            request_path.write_bytes(request_bytes)
            request = read_snapshot_request(request_path)
            if solve:
                report = calculate_field_power_flow(request, root, demo=demo)
                target = root / "output"
                write_snapshot_outputs(target, report)
                status = report["status"]
                if not isinstance(status, str) or status not in (
                    "secure",
                    "violation",
                    "invalid_input",
                    "not_converged",
                ):
                    raise ValueError("UNSUPPORTED_LEGACY_RESULT_STATUS")
                artifacts = tuple(
                    Artifact(path.name, path.read_bytes()) for path in sorted(target.iterdir())
                )
            else:
                snapshot = build_field_snapshot(request, root, demo=demo)
                status = "validated"
                validation = dict(
                    status=status,
                    target_at=at.isoformat(),
                    power_flow_performed=False,
                    network_safety="unknown",
                    field_acceptance_certified=False,
                )
                artifacts = (
                    Artifact(
                        "snapshot.json",
                        TypeAdapter(FixedStateSnapshot).dump_json(snapshot, indent=2),
                    ),
                    Artifact("validation.json", json.dumps(validation, indent=2).encode()),
                )
            return Evaluation(status, (Artifact("request.json", request_bytes), *artifacts))

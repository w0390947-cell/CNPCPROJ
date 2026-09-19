"""Fixed dataset workflow with captured inputs and explicit publication."""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from oilfield_energy.modules.measurements.api import CaptureDataset
from oilfield_energy.modules.measurements.contracts import CapturedDataset

from .contracts import Artifact, Operation, ResultStore, RunOutcome, SnapshotEngine


def archive_path(file_id: str, original_path: str) -> str:
    """Stable file-id based layout, independent of the source directory."""
    suffix = Path(original_path).suffix.lower()
    return f"files/{file_id}/content{suffix}"


def dataset_artifacts(dataset: CapturedDataset) -> tuple[Artifact, ...]:
    references = tuple(
        item.reference.model_copy(
            update={
                "path": archive_path(item.reference.file_id, item.reference.path),
                "sha256": item.sha256,
            }
        )
        for item in dataset.files
    )
    manifest = dataset.manifest.model_copy(update={"files": references})
    return (
        Artifact("dataset/manifest.json", manifest.model_dump_json(indent=2).encode()),
        Artifact("original_manifest.json", dataset.original_manifest),
        *(
            Artifact(f"dataset/{ref.path}", item.content)
            for ref, item in zip(references, dataset.files, strict=True)
        ),
    )


class FieldDatasetWorkflow:
    def __init__(
        self,
        capture: CaptureDataset,
        engine: SnapshotEngine,
        store: ResultStore,
        *,
        provenance: tuple[Artifact, ...] = (),
    ) -> None:
        self._capture = capture
        self._engine = engine
        self._store = store
        self._provenance = provenance

    def execute(
        self,
        manifest: Path,
        output: Path,
        *,
        operation: Operation,
        at: datetime,
        mode: str | None = None,
        demo: bool = False,
    ) -> RunOutcome:
        artifacts: tuple[Artifact, ...] = ()
        diagnostic: dict[str, object] = {
            "operation": operation,
            "target_at": at.isoformat(),
            "demo": demo,
            "redispatch_performed": False,
            "field_acceptance_certified": False,
        }
        try:
            if operation not in ("check", "seal", "run") or at.utcoffset() is None:
                raise ValueError("INVALID_OPERATION_OR_TIMEZONE_MISSING")
            captured = self._capture.capture(manifest, refresh_hashes=operation == "seal")
            artifacts = dataset_artifacts(captured)
            diagnostic.update(
                dataset_id=captured.manifest.dataset_id,
                revision=captured.manifest.revision,
                original_manifest_sha256=sha256(captured.original_manifest).hexdigest(),
            )
            evaluation = self._engine.evaluate(
                captured, at=at, mode=mode, demo=demo, solve=operation == "run"
            )
            artifacts += evaluation.artifacts
            status = evaluation.status
        except (ValueError, OSError, OverflowError) as exc:
            status = "invalid_input"
            diagnostic["reasons"] = [str(exc)]
        # check/seal validate this operating point; they do not certify network safety.
        code = 0 if status in ("validated", "secure", "violation") else 2
        diagnostic.update(status=status, exit_code=code)
        artifacts += self._provenance + (
            Artifact(
                "dataset_run.json",
                json.dumps(diagnostic, ensure_ascii=False, indent=2, allow_nan=False).encode(),
            ),
        )
        self._store.publish(output, artifacts)
        return RunOutcome(status, code, output)

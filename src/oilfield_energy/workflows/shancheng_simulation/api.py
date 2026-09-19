"""Capture once, execute the required checks, publish evidence without overwrite."""

import json
from pathlib import Path

from oilfield_energy.modules.measurements.api import CaptureDataset
from oilfield_energy.workflows.field_dataset.api import dataset_artifacts
from oilfield_energy.workflows.field_dataset.contracts import Artifact, ResultStore, RunOutcome

from .contracts import StudyEngine


class ShanchengSimulation:
    def __init__(
        self,
        capture: CaptureDataset,
        engine: StudyEngine,
        store: ResultStore,
        provenance: tuple[Artifact, ...],
    ) -> None:
        self.capture, self.engine, self.store, self.provenance = capture, engine, store, provenance

    def execute(self, manifest: Path, output: Path) -> RunOutcome:
        artifacts = self.provenance
        report: dict[str, object] = dict(synthetic=True, field_acceptance_certified=False)
        try:
            captured = self.capture.capture(manifest)
            if not captured.manifest.synthetic:
                raise ValueError("synthetic study refuses field data")
            artifacts += dataset_artifacts(captured)
            evaluation = self.engine.evaluate(captured)
            artifacts += evaluation.artifacts
            checks = dict(evaluation.checks)
            passed = bool(checks) and all(checks.values())
            report.update(
                checks=checks,
                passed=passed,
                dataset_id=captured.manifest.dataset_id,
                revision=captured.manifest.revision,
            )
        except (ValueError, OSError, RuntimeError) as exc:
            passed = False
            report.update(passed=False, error=str(exc))
        status = "passed" if passed else "failed"
        report["status"] = status
        artifacts += (
            Artifact(
                "study_summary.json",
                json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode(),
            ),
        )
        self.store.publish(output, artifacts)
        return RunOutcome(status, 0 if passed else 2, output)

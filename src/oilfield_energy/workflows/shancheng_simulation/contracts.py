"""Workflow-owned port for numerical study execution."""

from dataclasses import dataclass
from typing import Protocol

from oilfield_energy.modules.measurements.contracts import CapturedDataset
from oilfield_energy.workflows.field_dataset.contracts import Artifact


@dataclass(frozen=True)
class StudyEvaluation:
    checks: tuple[tuple[str, bool], ...]
    artifacts: tuple[Artifact, ...]


class StudyEngine(Protocol):
    def evaluate(self, dataset: CapturedDataset) -> StudyEvaluation: ...

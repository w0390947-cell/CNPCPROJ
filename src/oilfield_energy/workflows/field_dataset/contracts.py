"""Typed orchestration contracts; serialized artifacts are boundary payloads."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from oilfield_energy.modules.measurements.contracts import CapturedDataset

Operation = Literal["check", "seal", "run"]


@dataclass(frozen=True)
class Artifact:
    path: str
    content: bytes


@dataclass(frozen=True)
class Evaluation:
    status: str
    artifacts: tuple[Artifact, ...]


@dataclass(frozen=True)
class RunOutcome:
    status: str
    exit_code: int
    output: Path


class SnapshotEngine(Protocol):
    def evaluate(
        self, dataset: CapturedDataset, *, at: datetime, mode: str | None, demo: bool, solve: bool
    ) -> Evaluation: ...


class ResultStore(Protocol):
    def publish(self, output: Path, artifacts: tuple[Artifact, ...]) -> None: ...

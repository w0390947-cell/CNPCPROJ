"""Freeze inputs and software identity before numerical work, without changing it."""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import platform
from typing import Any, Literal

from oilfield_energy.data import ProjectCase
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy
from oilfield_energy.workflows.single_execution.contracts_provenance import SingleInputRecord
from .demo_case import encode
from .source_snapshot import source_digest


@dataclass(frozen=True)
class SingleRunCapture:
    record: SingleInputRecord

    def with_execution(self, inputs: dict[str, Any], basis: str) -> SingleInputRecord:
        payload = json.loads(self.record.input_snapshot_json)
        payload["execution_inputs"] = inputs
        snapshot = encode(payload).decode("utf-8")
        return SingleInputRecord(**{
            **self.record.model_dump(),
            "snapshot_scope": "reference_and_execution",
            "input_basis": basis,
            "input_snapshot_json": snapshot,
            "input_sha256": sha256(snapshot.encode("utf-8")).hexdigest(),
        })


def capture_single_inputs(
    case: ProjectCase, policy: ExecutionPolicy, inputs: dict[str, Any], *,
    phase: Literal["before_run", "before_execution"] = "before_run",
) -> SingleRunCapture:
    versions = {"python": platform.python_version()}
    for package in (
        "numpy", "scipy", "cvxpy", "pyscipopt", "clarabel", "highspy",
        "osqp", "scs", "threadpoolctl", "pydantic",
    ):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            continue
    snapshot = encode({
        "version": "single-input-snapshot-v1",
        "reference_case_after_events": case,
        "execution_policy": policy.model_dump(mode="json"),
        **inputs,
    }).decode("utf-8")
    return SingleRunCapture(SingleInputRecord(
        recorded_at=datetime.now(timezone.utc).isoformat(),
        code_snapshot_phase=phase, code_sha256=source_digest(),
        snapshot_scope="reference_only",
        input_snapshot_json=snapshot,
        input_sha256=sha256(snapshot.encode("utf-8")).hexdigest(),
        dataset_id=case.dataset_id, dataset_revision=case.dataset_revision,
        dataset_sha256=case.dataset_sha256,
        execution_version=policy.single_execution_version,
        runtime_versions=versions,
    ))

"""File/CLI composition of the same simulation service used by Web workers.

No planning, feedback, recovery or adoption decisions belong in this adapter.
Each run owns a fresh directory so failed runs cannot inherit successful evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

import oilfield_energy.service as service
from oilfield_energy.analysis import write_case_data
from oilfield_energy.data import ProjectCase


def _require_cluster(request: service.SimulationRequest) -> None:
    if request.scenario_type not in (
        service.ScenarioType.CLUSTER_COORDINATION,
        service.ScenarioType.COMMUNICATION_FAULT,
    ):
        raise ValueError("cluster studies require a cluster or communication-fault scenario")


def run_cluster_study(
    request: service.SimulationRequest,
    output: Path,
    *,
    input_case: ProjectCase | None = None,
    progress_callback: service.ProgressCallback | None = None,
) -> service.SimulationResult:
    """Run the authoritative service and archive its unmodified evidence, including failure."""
    request = service.SimulationRequest.model_validate(request.model_dump())
    _require_cluster(request)
    output.mkdir(parents=True, exist_ok=False)
    (output / "request.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
    # Explicit study cases are replay inputs too; the request alone cannot describe them.
    try:
        if input_case is not None:
            write_case_data(output / "input_case.json", input_case)
        result = service.run_simulation(request, progress_callback, input_case=input_case)
        (output / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        if result.cluster_execution is None:
            raise RuntimeError("cluster service returned no execution evidence")
        (output / "cluster_execution.json").write_text(
            result.cluster_execution.model_dump_json(indent=2), encoding="utf-8"
        )
    except Exception as exc:
        (output / "failure.json").write_text(
            json.dumps(
                {"error_type": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    return result


def run_cluster_batch(request_file: Path, output: Path) -> bool:
    """Archive independent requests; keep failed jobs and return false unless all pass."""
    requests = TypeAdapter(list[service.SimulationRequest]).validate_json(request_file.read_bytes())
    if not requests:
        raise ValueError("cluster batch requires at least one request")
    for request in requests:
        _require_cluster(request)
    output.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, str | None]] = []
    for index, request in enumerate(requests):
        directory = f"{index:04d}"
        try:
            result = run_cluster_study(request, output / directory)
            execution = result.cluster_execution
            records.append(
                {
                    "directory": directory,
                    "status": execution.status if execution else "unknown",
                    "algorithm_version": execution.algorithm_version if execution else None,
                }
            )
        except Exception as exc:
            records.append({"directory": directory, "status": "error", "reason": str(exc)})
        (output / "batch.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return all(record["status"] == "passed" for record in records)

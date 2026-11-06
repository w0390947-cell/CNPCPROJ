"""Exported initialization schemas match the implemented strict public models."""

import json
from pathlib import Path

from oilfield_energy.workflows.field_initialization.contracts import (
    DispatchControlCycleRequest,
    ExecutionResponse,
    InitializeRequest,
    InitializeResponse,
    PrepareControlCycleRequest,
    PrepareControlCycleResponse,
    PrepareResynchronizationRequest,
    QueryRunStatusRequest,
    ReportExecutionRequest,
    RequestPccTargetRequest,
    RequestPccTargetResponse,
    ResumeRunRequest,
    StopRunRequest,
    SubmitSnapshotRequest,
    SubmitSnapshotResponse,
    UpdatePlanningRequest,
    UpdatePlanningResponse,
)


def test_field_initialization_contracts_are_generated_from_models():
    root = Path(__file__).resolve().parents[2] / "contracts/field_initialization"
    for name, model in (
        ("initialize.request", InitializeRequest),
        ("initialize.response", InitializeResponse),
        ("planning-update.request", UpdatePlanningRequest),
        ("planning-update.response", UpdatePlanningResponse),
        ("pcc-target.request", RequestPccTargetRequest),
        ("pcc-target.response", RequestPccTargetResponse),
        ("control-cycle.request", PrepareControlCycleRequest),
        ("control-cycle.response", PrepareControlCycleResponse),
        ("snapshot.request", SubmitSnapshotRequest),
        ("snapshot.response", SubmitSnapshotResponse),
        ("execution-report.request", ReportExecutionRequest),
        ("execution-report.response", ExecutionResponse),
        ("run-status.request", QueryRunStatusRequest),
        ("run-status.response", ExecutionResponse),
        ("stop-run.request", StopRunRequest),
        ("stop-run.response", ExecutionResponse),
        ("dispatch-cycle.request", DispatchControlCycleRequest),
        ("dispatch-cycle.response", ExecutionResponse),
        ("resynchronization.request", PrepareResynchronizationRequest),
        ("resynchronization.response", ExecutionResponse),
        ("resume-run.request", ResumeRunRequest),
        ("resume-run.response", ExecutionResponse),
    ):
        assert (
            json.loads((root / f"{name}.schema.json").read_text(encoding="utf-8"))
            == model.model_json_schema()
        )

"""Export implemented field-session wire contracts without server startup."""

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "contracts/field_initialization"
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
        path = root / f"{name}.schema.json"
        content = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Generated initialization contract differs: {path}")
        else:
            root.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()

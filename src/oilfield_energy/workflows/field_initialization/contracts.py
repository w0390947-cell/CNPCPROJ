"""Public, strict initialization request, result and immutable session evidence."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from oilfield_energy.modules.control.contracts import TubeBox, TubeState
from oilfield_energy.workflows.single_execution.contracts import (
    UnifiedDomain,
    UnifiedPlanCertificate,
)

from .contracts_cycles import (
    ControlCycleEvidence as ControlCycleEvidence,
)
from .contracts_cycles import (
    PrepareControlCycleRequest as PrepareControlCycleRequest,
)
from .contracts_cycles import (
    PrepareControlCycleResponse as PrepareControlCycleResponse,
)
from .contracts_execution import (
    CommandResult as CommandResult,
)
from .contracts_execution import ConfirmedBoundary, RunState
from .contracts_execution import (
    DispatchControlCycleRequest as DispatchControlCycleRequest,
)
from .contracts_execution import (
    ExecutionBatch as ExecutionBatch,
)
from .contracts_execution import (
    ExecutionResponse as ExecutionResponse,
)
from .contracts_execution import (
    QueryRunStatusRequest as QueryRunStatusRequest,
)
from .contracts_execution import (
    ReportExecutionRequest as ReportExecutionRequest,
)
from .contracts_execution import (
    StopRunRequest as StopRunRequest,
)
from .contracts_execution import (
    TransportCapabilities as TransportCapabilities,
)
from .contracts_model import (
    ControlPolicy as ControlPolicy,
)
from .contracts_model import (
    ModelPackage as ModelPackage,
)
from .contracts_model import PointMapping as PointMapping
from .contracts_planning import PlanningInputs as PlanningInputs
from .contracts_recovery import (
    PrepareResynchronizationRequest as PrepareResynchronizationRequest,
)
from .contracts_recovery import QuiescenceEvidence as QuiescenceEvidence
from .contracts_recovery import ResumeRunRequest as ResumeRunRequest
from .contracts_snapshots import (
    ObservationBarrier as ObservationBarrier,
)
from .contracts_snapshots import (
    ObservationCursor as ObservationCursor,
)
from .contracts_snapshots import (
    SnapshotAssessment as SnapshotAssessment,
)
from .contracts_snapshots import (
    SnapshotDecision as SnapshotDecision,
)
from .contracts_snapshots import (
    SnapshotResponsePayload as SnapshotResponsePayload,
)
from .contracts_snapshots import (
    SnapshotResult as SnapshotResult,
)
from .contracts_snapshots import (
    SubmitSnapshotRequest as SubmitSnapshotRequest,
)
from .contracts_snapshots import (
    SubmitSnapshotResponse as SubmitSnapshotResponse,
)
from .contracts_snapshots import (
    SubmittedSnapshot as SubmittedSnapshot,
)
from .contracts_targets import (
    PccGoal as PccGoal,
)
from .contracts_targets import (
    PccTarget as PccTarget,
)
from .contracts_targets import (
    PccTargetEvidence as PccTargetEvidence,
)
from .contracts_targets import (
    PccTargetResponsePayload as PccTargetResponsePayload,
)
from .contracts_targets import (
    PccTargetResult as PccTargetResult,
)
from .contracts_targets import (
    RequestPccTargetRequest as RequestPccTargetRequest,
)
from .contracts_targets import (
    RequestPccTargetResponse as RequestPccTargetResponse,
)
from .contracts_timing import BoundedTimingPolicy as BoundedTimingPolicy
from .contracts_timing import TimedCycleProof as TimedCycleProof
from .contracts_uncertainty import MeasurementLimit as MeasurementLimit
from .contracts_uncertainty import (
    ObservationUncertaintyPolicy as ObservationUncertaintyPolicy,
)
from .contracts_updates import (
    PlanningUpdate as PlanningUpdate,
)
from .contracts_updates import (
    PlanningUpdatePayload as PlanningUpdatePayload,
)
from .contracts_updates import (
    PlanningUpdateResult as PlanningUpdateResult,
)
from .contracts_updates import (
    UpdatePlanningRequest as UpdatePlanningRequest,
)
from .contracts_updates import (
    UpdatePlanningResponse as UpdatePlanningResponse,
)
from .contracts_values import ArtifactReference as ArtifactReference
from .contracts_values import Authority as Authority
from .contracts_values import InitializationFailure as InitializationFailure
from .contracts_values import InterfaceError as InterfaceError
from .contracts_values import MeasurementUncertainty as MeasurementUncertainty
from .contracts_values import Name, Value
from .contracts_values import Snapshot as Snapshot
from .contracts_values import TimestampUncertainty as TimestampUncertainty

INTERFACE_VERSION = "single-microgrid-field-v0.1-draft"


class InitializePayload(Value):
    mode: Literal["shadow", "closed_loop"]
    model_package: ModelPackage
    initial_snapshot: Snapshot
    planning_inputs: PlanningInputs
    control_policy: ControlPolicy
    authority: Authority

    @model_validator(mode="after")
    def mode_permission(self) -> "InitializePayload":
        if self.mode == "shadow" and self.authority.write_enabled:
            raise ValueError("Shadow authority cannot enable writes")
        return self


class InitializeRequest(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"]
    operation: Literal["InitializeRun"]
    message_id: Name
    correlation_id: None
    microgrid_id: Name
    run_id: None
    created_at: AwareDatetime
    payload: InitializePayload


class InitializationResult(Value):
    run_id: str
    context_id: str
    run_state: Literal["ready"] = "ready"
    mode: Literal["shadow", "closed_loop"] = "shadow"
    dispatch_enabled: bool = False
    accepted_versions: dict[str, str]
    initial_safety_status: Literal["passed"] = "passed"
    energy_task_guarantee: Literal["not_required", "maintained", "unproven"]
    missing_requirements: tuple[str, ...] = (
        "Shadow mode does not grant dispatch permission; configure a supported software execution port for closed_loop.",
    )
    evidence_ref: str
    valid_until: AwareDatetime


class ResponsePayload(Value):
    outcome: Literal["accepted", "rejected"]
    errors: tuple[InterfaceError, ...] = ()
    data: InitializationResult | None = None


class InitializeResponse(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"] = INTERFACE_VERSION
    operation: Literal["InitializeRun"] = "InitializeRun"
    message_id: str
    correlation_id: str
    microgrid_id: str
    run_id: str | None
    created_at: AwareDatetime
    payload: ResponsePayload


class InitializationEvidence(Value):
    initial_box: TubeBox | None = None
    timed_proof: TimedCycleProof | None = None
    request: InitializeRequest
    resolved_model_package: ModelPackage
    domain: UnifiedDomain
    initial: TubeState
    plan: UnifiedPlanCertificate
    terminal_proof_digest: str
    valid_until: AwareDatetime
    completed_energy_task: bool = False
    # Read projection only: historical evidence is never rewritten by observations.
    observation_barrier: ObservationBarrier | None = None
    confirmed_boundary: ConfirmedBoundary | None = Field(default=None, exclude=True)
    field_run_state: RunState | None = Field(default=None, exclude=True)
    pending_cycle: ControlCycleEvidence | None = Field(default=None, exclude=True)
    pending_sent: bool = Field(default=False, exclude=True)

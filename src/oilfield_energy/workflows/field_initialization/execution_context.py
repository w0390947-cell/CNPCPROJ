"""Transaction inputs and explicit state decisions for field execution."""

from .contracts import (
    InitializationEvidence,
    ObservationCursor,
    PccTargetEvidence,
    SnapshotAssessment,
)
from .contracts_execution import (
    DispatchControlCycleRequest,
    ExecutionBatch,
    ExecutionLedger,
    ExecutionResponse,
    QueryRunStatusRequest,
    ReportExecutionRequest,
    StopRunRequest,
)
from .contracts_recovery import PrepareResynchronizationRequest, ResumeRunRequest
from .contracts_snapshots import SubmittedSnapshot
from .contracts_values import Value
from .recovery_evidence import ResynchronizationEvidence

ExecutionRequest = (
    ReportExecutionRequest
    | DispatchControlCycleRequest
    | QueryRunStatusRequest
    | StopRunRequest
    | PrepareResynchronizationRequest
    | ResumeRunRequest
)


class ExecutionContext(Value):
    current: InitializationEvidence | None
    target: PccTargetEvidence | None = None
    cursor: ObservationCursor | None = None
    ledger: ExecutionLedger = ExecutionLedger()
    batch: ExecutionBatch | None = None
    snapshot: SubmittedSnapshot | None = None
    assessment: SnapshotAssessment | None = None
    recovery: ResynchronizationEvidence | None = None
    recovery_consumed: bool = False
    reserved_revisions: frozenset[str] = frozenset()
    unsettled_batch_ids: frozenset[str] = frozenset()


class ExecutionDecision(Value):
    response: ExecutionResponse
    ledger: ExecutionLedger
    batch: ExecutionBatch | None = None
    recovery: ResynchronizationEvidence | None = None
    activate_recovery: bool = False

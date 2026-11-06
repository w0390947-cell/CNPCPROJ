"""Initialization owns its storage, artifact and network projection ports."""

from collections.abc import Callable
from typing import Protocol

from oilfield_energy.modules.power_flow.contracts import RadialEnvelope

from .contracts import (
    ArtifactReference,
    ControlCycleEvidence,
    ControlPolicy,
    InitializationEvidence,
    InitializeResponse,
    ObservationCursor,
    PccTargetEvidence,
    PrepareControlCycleRequest,
    PrepareControlCycleResponse,
    RequestPccTargetRequest,
    RequestPccTargetResponse,
    SnapshotDecision,
    SubmitSnapshotRequest,
    SubmitSnapshotResponse,
    UpdatePlanningRequest,
    UpdatePlanningResponse,
)
from .contracts_execution import (
    CommandResult,
    ExecutionBatch,
    ExecutionResponse,
    TransportCapabilities,
)
from .contracts_recovery import QuiescenceEvidence
from .contracts_values import Authority
from .execution_context import ExecutionContext, ExecutionDecision, ExecutionRequest


class ExecutionTransport(Protocol):
    @property
    def capabilities(self) -> TransportCapabilities: ...

    def authorized(self, microgrid_id: str, authority: Authority) -> bool: ...

    def send(self, microgrid_id: str, batch: ExecutionBatch) -> tuple[CommandResult, ...]: ...

    def query(self, microgrid_id: str, batch: ExecutionBatch) -> tuple[CommandResult, ...]: ...


class RecoveryVerifier(Protocol):
    def verify_quiescence(
        self, microgrid_id: str, authority: Authority, evidence: QuiescenceEvidence
    ) -> bool:
        """Verify trusted closure/fencing provenance; an unaudited client flag is insufficient."""
        ...


class ExecutionStore(Protocol):
    def recorded(self, request: ExecutionRequest) -> ExecutionResponse | None: ...

    def inspect(self, request: ExecutionRequest) -> ExecutionContext: ...

    def apply(
        self,
        request: ExecutionRequest,
        decide: Callable[[ExecutionContext], ExecutionDecision],
    ) -> tuple[ExecutionResponse, bool]: ...


class ArtifactReader(Protocol):
    def read(self, reference: ArtifactReference) -> bytes: ...


class ControlCycleStore(Protocol):
    def prepare_once(
        self,
        request: PrepareControlCycleRequest,
        fingerprint: str,
        identity: str,
        prepare: Callable[
            [
                InitializationEvidence | None,
                PccTargetEvidence | None,
                ObservationCursor | None,
                str | None,
                PrepareControlCycleResponse | None,
            ],
            tuple[PrepareControlCycleResponse, ControlCycleEvidence | None],
        ],
    ) -> PrepareControlCycleResponse: ...


class NetworkAssembler(Protocol):
    def assemble(self, content: bytes, mode: str, policy: ControlPolicy) -> RadialEnvelope: ...


class SessionStore(Protocol):
    def execute_once(
        self,
        microgrid_id: str,
        message_id: str,
        fingerprint: str,
        prepare: Callable[[], tuple[InitializeResponse, InitializationEvidence | None]],
    ) -> InitializeResponse: ...


class PlanningUpdateStore(Protocol):
    def update_once(
        self,
        request: UpdatePlanningRequest,
        fingerprint: str,
        prepare: Callable[
            [InitializationEvidence | None, frozenset[str], PccTargetEvidence | None],
            tuple[
                UpdatePlanningResponse,
                InitializationEvidence | None,
                PccTargetEvidence | None,
            ],
        ],
    ) -> UpdatePlanningResponse: ...


class PccTargetStore(Protocol):
    def request_once(
        self,
        request: RequestPccTargetRequest,
        fingerprint: str,
        identity: str,
        prepare: Callable[
            [InitializationEvidence | None, PccTargetEvidence | None, str | None],
            tuple[RequestPccTargetResponse, PccTargetEvidence | None],
        ],
    ) -> RequestPccTargetResponse: ...


class SnapshotStore(Protocol):
    def submit_once(
        self,
        request: SubmitSnapshotRequest,
        fingerprint: str,
        identity: str,
        prepare: Callable[
            [
                InitializationEvidence | None,
                PccTargetEvidence | None,
                ObservationCursor | None,
                SubmitSnapshotResponse | None,
            ],
            SnapshotDecision,
        ],
    ) -> SubmitSnapshotResponse: ...

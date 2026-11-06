"""Observed field snapshots and assessments, separate from execution confirmation."""

from typing import Literal

from pydantic import AwareDatetime

from oilfield_energy.modules.control.contracts import TubeBox, TubeState
from oilfield_energy.modules.power_flow.contracts import ACBoxCertificate
from oilfield_energy.workflows.single_execution.contracts import SingleForecastEnvelope

from .contracts_values import Bounds, InterfaceError, Name, SnapshotData, Value


class SubmittedSnapshot(SnapshotData):
    context_id: Name


class SubmitSnapshotRequest(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"]
    operation: Literal["SubmitSnapshot"]
    message_id: Name
    correlation_id: Name | None
    microgrid_id: Name
    run_id: Name
    created_at: AwareDatetime
    payload: SubmittedSnapshot


class ObservationBarrier(Value):
    snapshot_id: str
    guarantee_status: Literal["unconfirmed", "invalidated"]
    error: InterfaceError


class PointWatermark(Value):
    observed_until: AwareDatetime | None = None
    group: str
    entity_id: str
    field: str
    observed_at: AwareDatetime


class ObservationCursor(Value):
    snapshot: SubmittedSnapshot
    watermarks: tuple[PointWatermark, ...]


class SnapshotAssessment(Value):
    observed_box: TubeBox | None = None
    measurement_ranges: dict[str, Bounds] = {}
    aligned_ranges: dict[str, Bounds] = {}
    state_semantics: Literal["exact", "bounded_set"] = "exact"
    context_id: str
    step: int
    observed_state: TubeState | None = None
    observed_inputs: SingleForecastEnvelope | None = None
    network: ACBoxCertificate | None = None


class SnapshotResult(Value):
    observed_box: TubeBox | None = None
    state_semantics: Literal["exact", "bounded_set"] = "exact"
    snapshot_id: str
    context_id: str | None
    evaluated_at: AwareDatetime
    input_status: Literal["valid", "invalid"]
    state_status: Literal["consistent", "outside_model", "unconfirmed"]
    observation_status: Literal["latest", "archived", "rejected"]
    guarantee_status: Literal["not_assessed", "unconfirmed", "invalidated"]
    resync_required: bool
    errors: tuple[InterfaceError, ...] = ()
    evidence_ref: str | None
    mode: Literal["shadow", "closed_loop"] = "shadow"
    dispatch_enabled: Literal[False] = False
    execution_confirmed: Literal[False] = False


class SnapshotResponsePayload(Value):
    outcome: Literal["accepted", "rejected"]
    errors: tuple[InterfaceError, ...] = ()
    data: SnapshotResult


class SubmitSnapshotResponse(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"] = (
        "single-microgrid-field-v0.1-draft"
    )
    operation: Literal["SubmitSnapshot"] = "SubmitSnapshot"
    message_id: str
    correlation_id: str
    microgrid_id: str
    run_id: str
    created_at: AwareDatetime
    payload: SnapshotResponsePayload


class SnapshotDecision(Value):
    response: SubmitSnapshotResponse
    advance_observation: bool = False
    barrier: ObservationBarrier | None = None
    assessment: SnapshotAssessment | None = None
    cursor: ObservationCursor | None = None

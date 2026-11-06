"""Software execution facts and wire messages; no transport or storage objects."""

from typing import Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictFloat, model_validator

from oilfield_energy.modules.control.contracts import TubeBox, TubeState

from .contracts_cycles import ControlCycleEvidence, DeviceCommand
from .contracts_snapshots import SubmittedSnapshot
from .contracts_timing import TimedCycleProof
from .contracts_values import (
    Authority,
    InterfaceError,
    Name,
    Nonnegative,
    TimestampUncertainty,
    Value,
)

RunState = Literal["ready", "awaiting_feedback", "resync_required", "stopped"]


class ConfirmedBoundary(Value):
    state_evidence_ref: str | None = None
    state_box: TubeBox | None = None
    timed_continuation: TimedCycleProof | None = None
    snapshot: SubmittedSnapshot
    state: TubeState
    expected: TubeBox
    last_cycle_id: Name | None


class RecoverySettlement(Value):
    timestamp_uncertainty: TimestampUncertainty | None = None
    command_id: Name
    resource_id: Name
    disposition: Literal["applied", "rejected", "cancelled", "not_sent"]
    terminal_at: AwareDatetime
    source_ref: Name


class CommandResult(Value):
    timestamp_uncertainty: TimestampUncertainty | None = None
    command_id: Name
    resource_id: Name
    transport_status: Literal["accepted", "rejected", "unknown"]
    device_status: Literal["applied", "rejected", "unknown"]
    acknowledged_at: AwareDatetime | None
    applied_at: AwareDatetime | None
    echoed_p_setpoint_mw: StrictFloat | None
    echoed_q_setpoint_mvar: StrictFloat | None
    reason: str


class EnergyEvidence(Value):
    sample_observed_at: AwareDatetime | None = None
    # Additional conversion/integration methods require independently validated adapters.
    method: Literal["measured_energy", "bounded_estimate"]
    interval_start: AwareDatetime
    interval_end: AwareDatetime
    source_ref: Name
    energy_end_mwh: StrictFloat
    uncertainty_mwh: Nonnegative


class ExecutionReport(Value):
    context_id: Name
    cycle_id: Name
    command_batch_id: Name
    feedback_stage: Literal["transport", "device_ack", "cycle_end", "exception"]
    reported_at: AwareDatetime
    command_results: tuple[CommandResult, ...]
    observed_snapshot_id: Name | None
    energy_evidence: EnergyEvidence | None
    reason: str

    @model_validator(mode="after")
    def unique(self) -> "ExecutionReport":
        if len({x.command_id for x in self.command_results}) != len(
            self.command_results
        ):
            raise ValueError("Duplicate command result")
        if self.feedback_stage == "cycle_end" and not self.observed_snapshot_id:
            raise ValueError("Cycle-end feedback needs its observed snapshot")
        return self


class ExecutionEnvelope(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"]
    message_id: Name
    correlation_id: Name | None
    microgrid_id: Name
    run_id: Name
    created_at: AwareDatetime


class ReportExecutionRequest(ExecutionEnvelope):
    operation: Literal["ReportExecution"]
    payload: ExecutionReport


class DispatchRequest(Value):
    context_id: Name
    command_batch_id: Name
    authority: Authority


class DispatchControlCycleRequest(ExecutionEnvelope):
    operation: Literal["DispatchControlCycle"]
    payload: DispatchRequest


class StatusQuery(Value):
    command_batch_id: Name | None = None
    reconcile: StrictBool = False


class QueryRunStatusRequest(ExecutionEnvelope):
    operation: Literal["QueryRunStatus"]
    payload: StatusQuery


class StopRequest(Value):
    reason: Name
    requested_at: AwareDatetime
    authority: Authority


class StopRunRequest(ExecutionEnvelope):
    operation: Literal["StopRun"]
    payload: StopRequest


class ExecutionBatch(Value):
    reconciliation_timestamp_uncertainty: TimestampUncertainty | None = None
    evidence: ControlCycleEvidence
    commands: tuple[DeviceCommand, ...]
    status: Literal[
        "prepared",
        "sending",
        "sent",
        "confirmed",
        "unconfirmed",
        "cancelled",
        "reconciled",
    ] = "prepared"
    results: tuple[CommandResult, ...] = ()
    dispatch_deadline_at: AwareDatetime
    feedback_due_at: AwareDatetime
    send_claimed_at: AwareDatetime | None = None
    confirmed_snapshot_id: Name | None = None
    reason: str = ""
    reconciled_at: AwareDatetime | None = None
    settlements: tuple[RecoverySettlement, ...] = ()


class ExecutionLedger(Value):
    run_state: RunState = "ready"
    confirmed: ConfirmedBoundary | None = None
    pending_batch_id: Name | None = None
    stopped_at: AwareDatetime | None = None
    reason: str = ""
    energy_task_outcome: Literal[
        "pending", "met", "missed", "indeterminate", "not_required"
    ] = "pending"
    last_resynchronization_id: str | None = None
    recovery_expired_target_id: str | None = None
    control_revision: int = Field(default=0, ge=0)


class ExecutionStatus(Value):
    representative_state: TubeState | None = None
    state_evidence_ref: str | None = None
    confirmed_state_box: TubeBox | None = None
    state_semantics: Literal["exact", "bounded_set"] = "exact"
    timing_revision: str | None = None
    apply_not_before_at: AwareDatetime | None = None
    apply_not_after_at: AwareDatetime | None = None
    dispatch_deadline_at: AwareDatetime | None = None
    feedback_due_at: AwareDatetime | None = None
    validation_scope: Literal[
        "software_synchronous_only", "bounded_monotone_v1", "bounded_uncertain_v1"
    ] = "software_synchronous_only"
    mode: Literal["shadow", "closed_loop"] | None = None
    context_id: str | None = None
    run_state: RunState | None = None
    command_batch_id: str | None = None
    batch_status: str | None = None
    cycle_status: Literal["pending", "confirmed", "unconfirmed"] | None = None
    command_results: tuple[CommandResult, ...] = ()
    pending_batch_id: str | None = None
    confirmed_snapshot_id: str | None = None
    last_confirmed_cycle_id: str | None = None
    confirmed_state: TubeState | None = None
    stopped_at: AwareDatetime | None = None
    target_id: str | None = None
    target_response_deadline_at: AwareDatetime | None = None
    target_guaranteed_until: AwareDatetime | None = None
    energy_task_deadline_at: AwareDatetime | None = None
    energy_task_guarantee: Literal["maintained", "unproven", "not_required"] = (
        "not_required"
    )
    energy_task_outcome: Literal[
        "pending", "met", "missed", "indeterminate", "not_required"
    ] = "not_required"
    tracking_status: Literal["passed", "violated", "unproven", "not_assessed"] = (
        "not_assessed"
    )
    tracking_scope: Literal["last_confirmed_boundary"] = "last_confirmed_boundary"
    energy_outcome_scope: Literal["confirmed_boundary_history"] = (
        "confirmed_boundary_history"
    )
    evidence_ref: str | None = None
    reason: str = ""
    resynchronization_id: str | None = None
    proposed_context_id: str | None = None
    recovery_status: Literal["prepared", "resumed", "rejected"] | None = None
    recovery_expires_at: AwareDatetime | None = None
    recovery_expired_target_id: str | None = None


class ExecutionPayload(Value):
    outcome: Literal["accepted", "pending", "rejected"]
    errors: tuple[InterfaceError, ...] = ()
    data: ExecutionStatus


class ExecutionResponse(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"] = (
        "single-microgrid-field-v0.1-draft"
    )
    operation: Literal[
        "ReportExecution",
        "QueryRunStatus",
        "StopRun",
        "DispatchControlCycle",
        "PrepareResynchronization",
        "ResumeRun",
    ]
    message_id: str
    correlation_id: str
    microgrid_id: str
    run_id: str
    created_at: AwareDatetime
    payload: ExecutionPayload


class TransportCapabilities(Value):
    timing_policy_sha256: str | None = None
    observation_policy_sha256: str | None = None
    timing_profile: Literal[
        "software_synchronous_v1", "bounded_monotone_v1", "bounded_uncertain_v1"
    ]
    timing_revision: Name | None = None
    provenance_ref: Name

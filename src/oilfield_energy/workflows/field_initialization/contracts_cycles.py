"""Shadow command preparation contracts; predicted state is never confirmed state."""

from typing import Literal

from pydantic import AwareDatetime, StrictFloat, model_validator

from oilfield_energy.workflows.single_execution.contracts import (
    PreparedUnifiedCycle,
    SingleForecastEnvelope,
    UnifiedDomain,
    UnifiedPlanCertificate,
)

from .contracts_snapshots import SubmittedSnapshot
from .contracts_timing import TimedCycleProof
from .contracts_values import Authority, InterfaceError, Name, Value


class ControlCycle(Value):
    context_id: Name
    cycle_id: Name
    cycle_start_at: AwareDatetime
    cycle_end_at: AwareDatetime
    snapshot_id: Name
    authority: Authority


class PrepareControlCycleRequest(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"]
    operation: Literal["PrepareControlCycle"]
    message_id: Name
    correlation_id: Name | None
    microgrid_id: Name
    run_id: Name
    created_at: AwareDatetime
    payload: ControlCycle


class DeviceCommand(Value):
    command_id: str
    resource_id: str
    p_setpoint_mw: StrictFloat | None
    q_setpoint_mvar: StrictFloat | None

    @model_validator(mode="after")
    def writable_axis(self) -> "DeviceCommand":
        if self.p_setpoint_mw is None and self.q_setpoint_mvar is None:
            raise ValueError("A command needs at least one authorized axis")
        return self


class PredictedDeviceState(Value):
    resource_id: str
    p_mw: StrictFloat
    q_mvar: StrictFloat


class PredictedEndpoint(Value):
    device_states: tuple[PredictedDeviceState, ...]
    storage_energy_mwh: StrictFloat


class CycleModelCheck(Value):
    status: Literal["passed", "unproven"]
    evidence_ref: str | None
    guaranteed_until: AwareDatetime | None


class ControlCycleResult(Value):
    timed_proof: TimedCycleProof | None = None
    decision: Literal["shadow_only", "dispatchable", "withheld"]
    cycle_id: str
    context_id: str
    snapshot_id: str
    command_batch_id: str | None
    target_id: str | None
    apply_at: AwareDatetime | None
    cycle_end_at: AwareDatetime | None
    dispatch_deadline_at: AwareDatetime | None = None
    feedback_due_at: AwareDatetime | None = None
    authority: Authority
    commands: tuple[DeviceCommand, ...]
    predicted_endpoint: PredictedEndpoint | None
    model_check: CycleModelCheck
    strategy_source: Literal["retained_feedback", "backup", "terminal"] | None
    energy_feedback: bool = False
    target_response_deadline_at: AwareDatetime | None = None
    target_guaranteed_until: AwareDatetime | None = None
    energy_task_deadline_at: AwareDatetime | None = None
    reason: str
    evaluated_at: AwareDatetime
    time_basis: Literal[
        "synchronous_model_only", "software_synchronous_v1", "bounded_monotone_v1", "bounded_uncertain_v1"
    ] = "synchronous_model_only"
    mode: Literal["shadow", "closed_loop"] = "shadow"
    dispatch_enabled: bool = False
    execution_confirmed: Literal[False] = False

    @model_validator(mode="after")
    def decision_shape(self) -> "ControlCycleResult":
        if self.decision in ("shadow_only", "dispatchable"):
            if (
                not self.command_batch_id
                or not self.commands
                or self.predicted_endpoint is None
                or self.apply_at is None
                or self.cycle_end_at is None
                or self.model_check.status != "passed"
                or not self.model_check.evidence_ref
                or self.strategy_source is None
            ):
                raise ValueError("Prepared shadow batch requires prediction and evidence")
            if self.decision == "dispatchable" and (
                self.mode != "closed_loop"
                or not self.dispatch_enabled
                or self.dispatch_deadline_at is None
                or self.feedback_due_at is None
                or self.time_basis not in ("software_synchronous_v1", "bounded_monotone_v1", "bounded_uncertain_v1")
                or self.time_basis in ("bounded_monotone_v1", "bounded_uncertain_v1")
                and self.timed_proof is None
            ):
                raise ValueError(
                    "Dispatch requires explicit software execution timing and permission"
                )
            if self.decision == "shadow_only" and (self.dispatch_enabled or self.mode != "shadow"):
                raise ValueError("Shadow batches cannot permit dispatch")
        elif (
            self.command_batch_id is not None
            or self.commands
            or self.predicted_endpoint is not None
            or self.apply_at is not None
            or self.cycle_end_at is not None
            or self.model_check.status != "unproven"
        ):
            raise ValueError("Withheld cycle cannot contain executable commands or predictions")
        return self


class ControlCycleResponsePayload(Value):
    outcome: Literal["accepted", "rejected"]
    errors: tuple[InterfaceError, ...] = ()
    data: ControlCycleResult

    @model_validator(mode="after")
    def outcome_matches_decision(self) -> "ControlCycleResponsePayload":
        if (self.outcome == "accepted") != (self.data.decision in ("shadow_only", "dispatchable")):
            raise ValueError("Outcome must match the preparation decision")
        if (self.outcome == "rejected") != bool(self.errors):
            raise ValueError("Only rejected preparations must carry errors")
        return self


class PrepareControlCycleResponse(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"] = (
        "single-microgrid-field-v0.1-draft"
    )
    operation: Literal["PrepareControlCycle"] = "PrepareControlCycle"
    message_id: str
    correlation_id: str
    microgrid_id: str
    run_id: str
    created_at: AwareDatetime
    payload: ControlCycleResponsePayload


class ControlCycleEvidence(Value):
    timed_proof: TimedCycleProof | None = None
    request: PrepareControlCycleRequest
    command_batch_id: str
    snapshot: SubmittedSnapshot
    domain: UnifiedDomain
    plan: UnifiedPlanCertificate
    observed: SingleForecastEnvelope
    prepared: PreparedUnifiedCycle
    target_id: str | None
    target_response_deadline_at: AwareDatetime | None
    target_guaranteed_until: AwareDatetime | None

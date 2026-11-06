"""Explicit resynchronization proposals and independent resume requests."""

from typing import Literal

from pydantic import AwareDatetime, model_validator

from .contracts_execution import ExecutionEnvelope, RecoverySettlement
from .contracts_planning import PlanningInputs
from .contracts_snapshots import SubmittedSnapshot
from .contracts_values import Authority, Name, TimestampUncertainty, Value


class QuiescenceEvidence(Value):
    timestamp_uncertainty: TimestampUncertainty | None = None
    command_batch_id: Name
    previous_authority: Authority
    fenced_at: AwareDatetime
    settlements: tuple[RecoverySettlement, ...]
    source_ref: Name

    @model_validator(mode="after")
    def unique_commands(self) -> "QuiescenceEvidence":
        if len({x.command_id for x in self.settlements}) != len(self.settlements):
            raise ValueError("Duplicate recovery command settlement")
        return self


class ResynchronizationProposal(Value):
    context_id: Name
    resynchronization_id: Name
    pending_batch_id: Name | None
    snapshot: SubmittedSnapshot
    planning_inputs: PlanningInputs
    authority: Authority
    quiescence: QuiescenceEvidence | None
    reason: Name


class PrepareResynchronizationRequest(ExecutionEnvelope):
    operation: Literal["PrepareResynchronization"]
    payload: ResynchronizationProposal


class ResumeProposal(Value):
    context_id: Name
    resynchronization_id: Name
    proposed_context_id: Name
    authority: Authority


class ResumeRunRequest(ExecutionEnvelope):
    operation: Literal["ResumeRun"]
    payload: ResumeProposal

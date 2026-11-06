"""External PCC requests, immutable decisions and their reproducible evidence."""

from typing import Literal

from pydantic import AwareDatetime, StrictFloat

from oilfield_energy.workflows.single_execution.contracts import (
    UnifiedDomain,
    UnifiedPlanCertificate,
)

from .contracts_timing import TimedCycleProof
from .contracts_values import InterfaceError, Name, Positive, Value


class PccGoal(Value):
    p_import_mw: StrictFloat
    q_import_mvar: StrictFloat


class PccTarget(Value):
    context_id: Name
    target_id: Name
    target_p_import_mw: StrictFloat
    target_q_import_mvar: StrictFloat
    effective_from: AwareDatetime
    response_deadline_at: AwareDatetime
    hold_until: AwareDatetime
    p_tolerance_mw: Positive
    q_tolerance_mvar: Positive
    source_ref: Name


class RequestPccTargetRequest(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"]
    operation: Literal["RequestPccTarget"]
    message_id: Name
    correlation_id: Name | None
    microgrid_id: Name
    run_id: Name
    created_at: AwareDatetime
    payload: PccTarget


class PccTargetEvidence(Value):
    timed_proof: TimedCycleProof | None = None
    request: RequestPccTargetRequest
    context_id: str
    snapshot_id: str
    domain: UnifiedDomain
    plan: UnifiedPlanCertificate
    guaranteed_until: AwareDatetime


class PccTargetResult(Value):
    target_id: str
    target_status: Literal["accepted", "rejected"]
    requested_target: PccGoal
    accepted_target: PccGoal | None
    response_deadline_at: AwareDatetime
    guaranteed_until: AwareDatetime | None
    context_id: str | None
    previous_target_id: str | None
    previous_strategy_status: Literal[
        "none",
        "conditional",
        "expired",
        "context_changed",
        "unconfirmed",
        "not_assessed",
    ]
    reason: str
    evidence_ref: str | None
    energy_task_guarantee: Literal["not_assessed", "not_required", "maintained", "unproven"]
    mode: Literal["shadow", "closed_loop"] = "shadow"
    dispatch_enabled: Literal[False] = False


class PccTargetResponsePayload(Value):
    outcome: Literal["accepted", "rejected"]
    errors: tuple[InterfaceError, ...] = ()
    data: PccTargetResult


class RequestPccTargetResponse(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"] = (
        "single-microgrid-field-v0.1-draft"
    )
    operation: Literal["RequestPccTarget"] = "RequestPccTarget"
    message_id: str
    correlation_id: str
    microgrid_id: str
    run_id: str
    created_at: AwareDatetime
    payload: PccTargetResponsePayload

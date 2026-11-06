"""Planning update wire contracts; activation is distinct from receipt."""

from typing import Literal

from pydantic import AwareDatetime

from .contracts_planning import PlanningData
from .contracts_values import InterfaceError, Name, Value


class PlanningUpdate(PlanningData):
    context_id: Name


class UpdatePlanningRequest(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"]
    operation: Literal["UpdatePlanningInputs"]
    message_id: Name
    correlation_id: Name | None
    microgrid_id: Name
    run_id: Name
    created_at: AwareDatetime
    payload: PlanningUpdate


class PlanningUpdateResult(Value):
    input_revision: str
    activation_status: Literal["staged", "activated", "rejected"]
    new_context_id: str | None
    effective_from: AwareDatetime
    valid_until: AwareDatetime
    preserved_task_id: str | None
    preserved_target_deadline_at: AwareDatetime | None = None
    reason: str
    evidence_ref: str | None
    mode: Literal["shadow", "closed_loop"] = "shadow"
    dispatch_enabled: Literal[False] = False
    energy_task_guarantee: Literal["not_assessed", "not_required", "maintained", "unproven"]


class PlanningUpdatePayload(Value):
    outcome: Literal["accepted", "rejected"]
    errors: tuple[InterfaceError, ...] = ()
    data: PlanningUpdateResult


class UpdatePlanningResponse(Value):
    interface_version: Literal["single-microgrid-field-v0.1-draft"] = (
        "single-microgrid-field-v0.1-draft"
    )
    operation: Literal["UpdatePlanningInputs"] = "UpdatePlanningInputs"
    message_id: str
    correlation_id: str
    microgrid_id: str
    run_id: str
    created_at: AwareDatetime
    payload: PlanningUpdatePayload

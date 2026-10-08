"""Versioned evidence for the sole formal unified single-microgrid execution."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oilfield_energy.modules.control.contracts import (
    DeviceCheckpoint,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    ExecutionStatus,
)

from .contracts_domain import (
    BoundedBusLoad as BoundedBusLoad,
)
from .contracts_domain import (
    FirmGeneration as FirmGeneration,
)
from .contracts_domain import (
    SingleBoundaryRequest as SingleBoundaryRequest,
)
from .contracts_tasks import (
    HardTaskAssessment as HardTaskAssessment,
)
from .contracts_tasks import (
    TaskContinuation as TaskContinuation,
)
from .contracts_tasks import (
    TerminalEnergyTask as TerminalEnergyTask,
)
from .contracts_unified import (
    SingleEconomicProposal as SingleEconomicProposal,
)
from .contracts_unified import (
    SingleForecastEnvelope as SingleForecastEnvelope,
)
from .contracts_unified import (
    SingleResearchPolicy as SingleResearchPolicy,
)
from .contracts_unified import (
    TargetAdmission as TargetAdmission,
)
from .contracts_unified import (
    TargetTrackingCertificate as TargetTrackingCertificate,
)
from .contracts_unified import (
    UnifiedDomain as UnifiedDomain,
)
from .contracts_unified import (
    UnifiedExecution as UnifiedExecution,
)
from .contracts_unified import (
    UnifiedPlanCertificate as UnifiedPlanCertificate,
)
from .contracts_unified import (
    UnifiedStep as UnifiedStep,
)


class SingleExecution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["single-execution-v9"]
    scope: Literal["synthetic_execution"] = "synthetic_execution"
    region: str
    status: ExecutionStatus
    reason: str = ""
    storage_enabled: bool
    policy: ExecutionPolicy
    input_basis: str
    initialization: Literal["declared_actual_state", "declared_safe_state"] = (
        "declared_safe_state"
    )
    startup_transition_assessed: Literal[False] = False
    initial_state: DeviceCheckpoint | None = None
    last_completed_state: DeviceCheckpoint | None = None
    total_minutes: int = Field(gt=0)
    completed_minutes: int = Field(default=0, ge=0)
    unified: UnifiedExecution | None = None

    @model_validator(mode="after")
    def causal_evidence(self) -> "SingleExecution":
        if self.unified is not None:
            evidence = self.unified
            if self.policy.single_execution_version != evidence.version:
                raise ValueError("unified execution requires its own policy")
            if (
                self.completed_minutes != len(evidence.steps)
                or self.total_minutes != evidence.requested_steps
            ):
                raise ValueError("unified execution clock differs")
            if self.status == "passed" and (
                evidence.status != "passed"
                or evidence.tracking_status != "passed"
                or evidence.target_admission_status != "passed"
                or evidence.hard_task.guarantee not in ("maintained", "not_required")
                or evidence.hard_task.outcome not in ("met", "not_required")
            ):
                raise ValueError(
                    "execution success requires both safety and tracking evidence"
                )
            resources = evidence.domain.devices.resources
            storage = next(i for i, r in enumerate(resources) if r.kind == "storage")
            last = evidence.steps[-1].after if evidence.steps else evidence.initial
            for checkpoint, state, minute in (
                (self.initial_state, evidence.initial, 0),
                (self.last_completed_state, last, len(evidence.steps)),
            ):
                if (
                    checkpoint is None
                    or checkpoint.minute != minute
                    or checkpoint.storage_energy_mwh != state.energy_mwh
                    or checkpoint.storage_power_mw != state.p_mw[storage]
                    or checkpoint.reactive_power_mvar
                    != {
                        r.resource_id: q
                        for r, q in zip(resources, state.q_mvar, strict=True)
                    }
                ):
                    raise ValueError("unified checkpoint differs from executed state")
            return self
        if (
            self.status != "not_computed"
            or self.completed_minutes
            or self.initial_state is not None
            or self.last_completed_state is not None
        ):
            raise ValueError("executed results require the unified certificate chain")
        return self

"""Display events observe committed simulation state; they never certify a run."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .contracts import ExecutionPolicy, RollingUpdate
from .contracts_safety import ClusterSafetyDomain, ClusterSafetyStep
from oilfield_energy.modules.dispatch.contracts import ReserveRecoveryTask
from .contracts_reserve import ReserveClusterCertificate


class DisplayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(ge=1)


class ClusterInitial(DisplayEvent):
    kind: Literal["cluster_initial"]
    total: int = Field(gt=0)
    domain: ClusterSafetyDomain
    policy: ExecutionPolicy
    observations: tuple[ReserveClusterCertificate, ...]
    tasks: tuple[ReserveRecoveryTask, ...]


class ClusterCycle(DisplayEvent):
    kind: Literal["cluster_cycle"]
    minute: int = Field(gt=0)
    step: ClusterSafetyStep
    pcc: dict[str, tuple[float, float, float | None, float | None]]
    tasks: tuple[ReserveRecoveryTask, ...]


class ClusterWindow(DisplayEvent):
    kind: Literal["cluster_window"]
    window: RollingUpdate
    observations: tuple[ReserveClusterCertificate, ...]
    tasks: tuple[ReserveRecoveryTask, ...]

"""Immutable mathematical prediction, separate from confirmed execution."""

from pydantic import BaseModel, ConfigDict

from oilfield_energy.modules.control.contracts import (
    TubeBox,
    TubeCommand,
    TubeState,
    TubeTransition,
)
from oilfield_energy.modules.power_flow.contracts import ACBoxCertificate


class PreparedUnifiedCycle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    command: TubeCommand
    predicted_endpoint: TubeState
    transition: TubeTransition
    path_network: ACBoxCertificate
    endpoint_network: ACBoxCertificate
    expected_next: TubeBox
    feedback_policy: bool
    energy_feedback: bool

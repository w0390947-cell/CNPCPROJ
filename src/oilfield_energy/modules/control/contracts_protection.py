"""Protection intent and command evidence; neither is proof of execution."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ProtectionTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    required_cap_mw: float = Field(ge=0)
    minimum_mw: float = Field(ge=0)
    maximum_mw: float = Field(ge=0)
    target_mw: float = Field(ge=0)
    capability_shortfall_mw: float = Field(ge=0)


class ProtectionTargetRecord(BaseModel):
    """One snapshot evaluation, distinct from the last PCC deficit allocation.

    Capability shortfall is excess above the retained wind/storage cap. Without
    fresh PCC input it cannot be interpreted as the current PCC import deficit.
    ``command_accepted=None`` means no new command was submitted this cycle.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    version: Literal["protection-target-v1"] = "protection-target-v1"
    snapshot_id: str
    observed_at_utc: AwareDatetime
    action: Literal["allocation", "renewal", "observation"]
    decision_id: str | None = None
    command_source_id: str | None = None
    command_source_epoch: str | None = None
    command_sequence: int | None = Field(default=None, ge=1)
    command_valid_until_utc: AwareDatetime | None = None
    previous_cap_mw: float | None = Field(default=None, ge=0)
    required_cap_mw: float | None = Field(default=None, ge=0)
    retained_cap_mw: float | None = Field(default=None, ge=0)
    projection: ProtectionTarget | None = None
    submitted_target_mw: float | None = Field(default=None, ge=0)
    command_accepted: bool | None = None
    controller_target_mw: float | None = Field(default=None, ge=0)
    controller_disposition: str
    reason: str

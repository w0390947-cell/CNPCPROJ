"""Planning bounds and soft reference, separate from physical storage limits."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StorageWindowTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    minimum_mwh: float
    maximum_mwh: float
    reference_mwh: float
    reference_cost_cny_per_mwh: float = Field(ge=0)

    @model_validator(mode="after")
    def nonempty(self) -> "StorageWindowTarget":
        if self.minimum_mwh > self.maximum_mwh:
            raise ValueError("empty terminal energy planning interval")
        return self

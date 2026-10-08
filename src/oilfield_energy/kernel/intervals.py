"""Finite closed real intervals shared by numerical domain contracts."""

from pydantic import BaseModel, ConfigDict, model_validator


class Interval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    low: float
    high: float

    @model_validator(mode="after")
    def ordered(self) -> "Interval":
        if self.low > self.high:
            raise ValueError("interval is reversed")
        return self

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

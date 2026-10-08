"""Persistent reserve obligations and the declared reserve service product."""

# pyright: strict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReserveDeliveryPolicy(BaseModel):
    """One-minute causal sampled service; see Reserve_Recovery_Delivery.md."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["reserve-delivery-box-v1"] = "reserve-delivery-box-v1"
    response_minutes: Literal[1] = 1
    activation_slew_fraction_per_minute: float = Field(default=1.0, gt=0, le=1)
    pcc_tolerance_mw: float = Field(default=1e-5, gt=0, le=0.001)
    response_roundoff_mw: float = Field(default=1e-9, gt=0, le=1e-6)
    correction_rounds: int = Field(default=2, ge=0, le=4)
    source: str = "研究要求：一分钟内响应；并非现场核定值"


class ReserveRecoveryTask(BaseModel):
    """An observed obligation survives every planning window and solver retry."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    region: str = Field(min_length=1)
    direction: Literal["up", "down"]
    first_minute: int = Field(ge=0)
    deadline_minute: int = Field(ge=0)
    last_observed_minute: int = Field(ge=0)
    required_mw: float = Field(ge=0)
    status: Literal["recovering", "recovered", "violated", "unknown"]
    completed_minute: int | None = Field(default=None, ge=0)
    breached: bool = False
    deadline_status: Literal["pending", "met", "missed", "unknown"] = "pending"
    reason: str = ""

    @model_validator(mode="after")
    def chronology(self) -> "ReserveRecoveryTask":
        if (
            self.deadline_minute <= self.first_minute
            or self.last_observed_minute < self.first_minute
        ):
            raise ValueError("invalid reserve recovery chronology")
        if (
            self.completed_minute is not None
            and not self.first_minute
            <= self.completed_minute
            <= self.last_observed_minute
        ):
            raise ValueError("invalid reserve completion time")
        if self.status == "recovered" and self.completed_minute is None:
            raise ValueError("recovery requires observed completion")
        if self.deadline_status == "met" and (
            self.completed_minute is None
            or self.completed_minute > self.deadline_minute
        ):
            raise ValueError("deadline met requires timely observed completion")
        if self.breached != (self.deadline_status == "missed"):
            raise ValueError("reserve breach and deadline evidence disagree")
        return self

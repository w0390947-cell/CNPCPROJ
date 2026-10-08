"""Replayable inputs and sufficient certificates for the reserve service."""

# pyright: strict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oilfield_energy.modules.dispatch.contracts import ReserveDeliveryPolicy
from oilfield_energy.modules.power_flow.contracts import (
    ACBoxCertificate,
    PowerInterval,
    RadialEnvelope,
)


class ReserveResourceBox(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    resource_id: str
    p_mw: PowerInterval
    q_mvar: PowerInterval
    s_mva: float = Field(gt=0)
    q_over_p: float | None = Field(default=None, ge=0)


class ReserveServiceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    region: str
    start_minute: int = Field(ge=0)
    support_minutes: int = Field(gt=0)
    policy: ReserveDeliveryPolicy
    network: RadialEnvelope
    # Demand excluding the storage active injection, including all other devices.
    demand_p_mw: tuple[PowerInterval, ...]
    demand_q_mvar: tuple[PowerInterval, ...]
    storage_bus: int = Field(ge=0)
    baseline_power_mw: PowerInterval
    initial_power_mw: float
    initial_energy_mwh: float
    initial_energy_bounds_mwh: PowerInterval | None = None
    minimum_energy_mwh: float
    maximum_energy_mwh: float
    maximum_power_mw: float = Field(gt=0)
    storage_s_mva: float = Field(gt=0)
    storage_q_mvar: PowerInterval
    eta_charge: float = Field(gt=0, le=1)
    eta_discharge: float = Field(gt=0, le=1)
    response_alpha: float = Field(gt=0, le=1)
    ramp_mw_per_minute: float = Field(ge=0)
    required_mw: float = Field(ge=0)
    # A device-side bracket, not the promised PCC reserve.
    activation_headroom_mw: float = Field(ge=0)
    context_id: str = Field(min_length=1)
    other_resources: tuple[ReserveResourceBox, ...] = ()

    @model_validator(mode="after")
    def dimensions(self) -> "ReserveServiceInput":
        if (
            len(self.demand_p_mw) != len(self.network.buses)
            or len(self.demand_q_mvar) != len(self.network.buses)
            or self.storage_bus >= len(self.network.buses)
        ):
            raise ValueError("reserve service bus axis differs")
        if (
            not self.minimum_energy_mwh
            <= self.initial_energy_mwh
            <= self.maximum_energy_mwh
        ):
            raise ValueError("reserve service starts outside physical energy bounds")
        return self


class ReserveServiceCertificate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    model: Literal["reserve-delivery-box-v1"] = "reserve-delivery-box-v1"
    inputs: ReserveServiceInput
    status: Literal["certified", "unknown"]
    reason: str = ""
    storage_power_mw: PowerInterval
    energy_mwh: PowerInterval
    command_mw: PowerInterval
    maximum_change_mw: float = Field(ge=0)
    network: ACBoxCertificate
    nominal_network: ACBoxCertificate
    up_endpoint: ACBoxCertificate
    down_endpoint: ACBoxCertificate

    @model_validator(mode="after")
    def complete_proof(self) -> "ReserveServiceCertificate":
        if self.status == "certified" and (
            self.reason
            or not all(
                c.certified
                for c in (
                    self.network,
                    self.nominal_network,
                    self.up_endpoint,
                    self.down_endpoint,
                )
            )
            or self.energy_mwh.low < self.inputs.minimum_energy_mwh
            or self.energy_mwh.high > self.inputs.maximum_energy_mwh
            or max(abs(self.command_mw.low), abs(self.command_mw.high))
            > self.inputs.maximum_power_mw
            or self.maximum_change_mw > self.inputs.ramp_mw_per_minute
        ):
            raise ValueError(
                "certified reserve service requires complete compatible evidence"
            )
        return self


class ReserveClusterCertificate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    start_minute: int
    status: Literal["certified", "unknown"]
    reason: str = ""
    regions: tuple[ReserveServiceCertificate, ...]
    import_upper_mw: float | None = None
    import_limit_mw: float
    pf_target: float

    @model_validator(mode="after")
    def complete_proof(self) -> "ReserveClusterCertificate":
        if self.status == "certified" and (
            self.reason
            or not self.regions
            or any(
                r.status != "certified" or r.inputs.start_minute != self.start_minute
                for r in self.regions
            )
            or len({r.inputs.region for r in self.regions}) != len(self.regions)
            or self.import_upper_mw is None
            or self.import_upper_mw > self.import_limit_mw
        ):
            raise ValueError(
                "certified cluster reserve requires aligned complete regional evidence"
            )
        return self


class ReserveActivationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    context_id: str
    requested_pcc_up_mw: float
    target_storage_mw: float
    command_storage_mw: float
    pcc_response_mw: float
    elapsed_minutes: int = Field(ge=0)

"""Explicit response model for unified single-microgrid execution."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oilfield_energy.kernel.intervals import Interval


class TubeValue(BaseModel, frozen=True):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class TubeResource(TubeValue, frozen=True):
    resource_id: str = Field(min_length=1)
    bus: str = Field(min_length=1)
    kind: Literal["wind", "pv", "storage", "svg"]
    p: Interval
    q: Interval
    s_mva: float = Field(gt=0)
    q_over_p: float | None = Field(default=None, ge=0)
    alpha_p: float = Field(gt=0, le=1)
    ramp_p_mw: float = Field(ge=0)


class TubeDeviceModel(TubeValue, frozen=True):
    version: Literal["unified-device-v1", "cluster-device-v1"] = "unified-device-v1"
    resources: tuple[TubeResource, ...]
    alpha_q: float = Field(gt=0, le=1)
    aggregate_q_ramp_mvar: float = Field(ge=0)
    step_minutes: float = Field(gt=0)
    energy_mwh: Interval
    eta_charge: float = Field(gt=0, le=1)
    eta_discharge: float = Field(gt=0, le=1)
    context_id: str = Field(min_length=1)
    settling_resolution: float = Field(default=0.0001, gt=0, le=0.001)

    @model_validator(mode="after")
    def identities(self) -> "TubeDeviceModel":
        ids = [r.resource_id for r in self.resources]
        storage = [r for r in self.resources if r.kind == "storage"]
        if not ids or len(ids) != len(set(ids)) or len(storage) != 1 or (self.version == "unified-device-v1" and storage[0].alpha_p != 1):
            raise ValueError("tube requires unique resources and one zero-order-hold PCS")
        return self


class TubeState(TubeValue, frozen=True):
    p_mw: tuple[float, ...]
    q_mvar: tuple[float, ...]
    energy_mwh: float


class TubeBox(TubeValue, frozen=True):
    p_mw: tuple[Interval, ...]
    q_mvar: tuple[Interval, ...]
    energy_mwh: Interval


class TubeCommand(TubeValue, frozen=True):
    p_mw: tuple[float, ...]
    q_mvar: tuple[float, ...]


class TubeTransition(TubeValue, frozen=True):
    initial: TubeBox
    command: TubeCommand
    endpoint: TubeBox
    path: TubeBox

"""Versioned simulator boundary; P/Q are injections, time is explicit UTC."""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


Fault = Literal[
    "normal",
    "communication_loss",
    "bad_quality",
    "voltage_sag",
    "ack_timeout",
    "load_drop",
    "wind_trip",
    "nonconvergence",
]


class DeviceSpec(Model):
    device_id: str = Field(min_length=1)
    region: str
    bus_id: str
    kind: Literal["wind", "pv", "storage", "svg"]
    p_min_mw: float
    p_max_mw: float
    q_max_mvar: float = Field(ge=0)
    s_max_mva: float = Field(gt=0)
    ramp_mw_per_minute: float = Field(gt=0)
    energy_mwh: float = Field(default=0, ge=0)
    minimum_mwh: float = Field(default=0, ge=0)
    initial_mwh: float = Field(default=0, ge=0)
    eta_charge: float = Field(default=0.95, gt=0, le=1)
    eta_discharge: float = Field(default=0.95, gt=0, le=1)

    @model_validator(mode="after")
    def limits(self) -> "DeviceSpec":
        if self.p_min_mw > self.p_max_mw:
            raise ValueError("inverted P bounds")
        if (
            max(abs(self.p_min_mw), abs(self.p_max_mw), self.q_max_mvar)
            > self.s_max_mva
        ):
            raise ValueError("P/Q capability exceeds apparent power rating")
        if self.kind in {"wind", "pv"} and self.p_min_mw != 0:
            raise ValueError("renewable P lower bound must be zero")
        if self.kind == "svg" and (self.p_min_mw != 0 or self.p_max_mw != 0):
            raise ValueError("SVG cannot produce active power")
        if not self.minimum_mwh <= self.initial_mwh <= self.energy_mwh:
            raise ValueError("invalid initial energy")
        if self.kind == "storage" and self.energy_mwh <= 0:
            raise ValueError("storage requires energy")
        return self


class Bundle(Model):
    schema_version: Literal["oilfield-demo-v1"]
    dataset_id: str = Field(min_length=1)
    synthetic: Literal[True]
    start: AwareDatetime
    source: str
    case: dict[str, JsonValue]
    devices: tuple[DeviceSpec, ...]
    presets: dict[str, dict[str, JsonValue]]

    @model_validator(mode="after")
    def unique_devices(self) -> "Bundle":
        if not self.devices or len({d.device_id for d in self.devices}) != len(
            self.devices
        ):
            raise ValueError("devices must be nonempty and unique")
        return self


class Command(Model):
    command_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    epoch: str
    device_id: str
    p_mw: float
    q_mvar: float
    expires_at: AwareDatetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def decode_time(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class Receipt(Model):
    command: Command
    status: Literal[
        "accepted",
        "executing",
        "executed",
        "rejected",
        "expired",
        "timeout",
        "superseded",
    ]
    reason: str
    updated_at: AwareDatetime


class Reading(Model):
    device_id: str
    region: str
    bus_id: str
    kind: str
    p_mw: float
    q_mvar: float
    available_mw: float
    energy_mwh: float | None


class Safety(Model):
    region: str
    status: str
    valid: bool
    recovery_safe: bool
    reasons: tuple[str, ...]
    pcc_import_mw: float | None = None
    pcc_q_mvar: float | None = None
    loss_mw: float | None = None
    voltage_min_pu: float | None = None
    voltage_max_pu: float | None = None
    max_loading_pu: float | None = None
    power_factor: float | None = None
    flow: dict[str, JsonValue] | None = None


class Frame(Model):
    schema_version: Literal["oilfield-telemetry-v1"] = "oilfield-telemetry-v1"
    synthetic: Literal[True] = True
    dataset_id: str
    epoch: str
    sequence: int
    simulated_at: AwareDatetime
    observed_at: AwareDatetime
    fault: Fault
    quality_valid: bool
    recovery_blocked: bool
    safe_cycles: int
    devices: tuple[Reading, ...]
    networks: tuple[Safety, ...]
    receipts: tuple[Receipt, ...]


class FaultRequest(Model):
    fault: Fault


class Catalog(Model):
    dataset_id: str
    synthetic: Literal[True] = True
    source: str
    presets: dict[str, dict[str, JsonValue]]
    devices: tuple[DeviceSpec, ...]


class AvailabilityPoint(Model):
    minute_of_day: int = Field(ge=0, lt=1440)
    p_available_mw: float = Field(ge=0)


class DeviceAvailabilitySeries(Model):
    device_id: str = Field(min_length=1)
    region: str
    kind: Literal["wind", "pv"]
    resolution_minutes: Literal[15] = 15
    points: tuple[AvailabilityPoint, ...] = Field(min_length=96, max_length=96)


class ResourceAvailability(Model):
    """Read-only, per-device renewable capability from the fixed demo bundle."""

    schema_version: Literal["oilfield-resource-availability-v1"] = (
        "oilfield-resource-availability-v1"
    )
    synthetic: Literal[True] = True
    source: Literal["synthetic_bundle_profile"] = "synthetic_bundle_profile"
    dataset_id: str = Field(min_length=1)
    period_minutes: Literal[1440] = 1440
    series: tuple[DeviceAvailabilitySeries, ...]


class NetworkPort(Protocol):
    def screen_contingencies(self) -> dict[str, JsonValue]: ...

    def available(self, device: DeviceSpec, minute: int) -> float: ...

    def evaluate(
        self,
        readings: tuple[Reading, ...],
        minute: int,
        at: AwareDatetime,
        observed: AwareDatetime,
        fault: Fault,
    ) -> tuple[Safety, ...]: ...


class JobsPort(Protocol):
    def health(self) -> dict[str, JsonValue]: ...
    def create(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]: ...
    def get(self, job_id: str) -> dict[str, JsonValue]: ...
    def cancel(self, job_id: str) -> dict[str, JsonValue]: ...
    def result(self, job_id: str) -> dict[str, JsonValue]: ...
    def tick(self) -> None: ...
    def close(self) -> None: ...

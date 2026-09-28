"""Explicit synthetic plant inputs, separate from commands and observed output."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, model_validator


class DynamicTrackingPolicy(BaseModel):
    """Offline response assessment parameters, shared with the synthetic plant."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    time_constant_minutes: float = Field(default=2.0, gt=0)
    pv_time_constant_minutes: float = Field(default=0.25, gt=0)
    p_ramp_mw_per_minute: float = Field(default=0.65, ge=0)
    q_ramp_mvar_per_minute: float = Field(default=0.90, ge=0)
    maximum_response_minutes: int = Field(default=10, ge=1)
    confirmation_samples: int = Field(default=3, ge=2)
    p_change_threshold_mw: float = Field(default=1e-4, gt=0)
    q_change_threshold_mvar: float = Field(default=1e-4, gt=0)
    source: str = "软件仿真动态判据：依据已记录的响应、爬坡、最长等待和连续确认参数；非现场验收定值"


TrackingStatus = Literal["passed", "violated", "unknown"]


@dataclass(frozen=True)
class ReactiveTrackingPoint:
    """Predicted, arbitrated ONE-cycle response; not executed telemetry."""

    controls: tuple[float, ...]
    targets: tuple[float, ...]
    responses: tuple[float, ...]
    pcc_q_mvar: float | None
    safe: bool


@dataclass(frozen=True)
class ReactiveTrackingResult:
    point: ReactiveTrackingPoint
    initial_error_mvar: float | None
    final_error_mvar: float | None
    evaluations: int
    status: Literal["improved", "limited", "held", "blocked"]
    version: Literal["reactive-tracking-v1"] = "reactive-tracking-v1"

# Numerical action comparisons in MW; neither is an engineering tracking limit.
ACTIVE_TRACKING_TRIGGER_MW = 1e-6
ACTIVE_TRACKING_MOVEMENT_MW = 1e-9


@dataclass(frozen=True)
class ActiveTrackingResource:
    """One device/group's authorized injection envelope, MW; positive is generation.

    previous is the start-of-cycle physical output; baseline is the provisional
    response to the plan. Bounds already include availability and owned caps.
    alpha and ramp describe ONE complete cycle, not another response allowance.
    """

    resource_id: str
    previous_mw: float
    baseline_mw: float
    minimum_mw: float
    maximum_mw: float
    ramp_mw: float
    alpha: float
    controllable: bool = True

    def __post_init__(self) -> None:
        if type(self.resource_id) is not str or not self.resource_id.strip():
            raise ValueError("active tracking requires a resource ID")
        values = (self.previous_mw, self.baseline_mw, self.minimum_mw,
                  self.maximum_mw, self.ramp_mw, self.alpha)
        if any(isinstance(v, bool) or not isfinite(v) for v in values):
            raise ValueError("active tracking inputs must be finite")
        if self.minimum_mw > self.maximum_mw or self.ramp_mw < 0 or not 0 < self.alpha <= 1:
            raise ValueError("invalid active tracking envelope or dynamics")
        if type(self.controllable) is not bool:
            raise ValueError("controllability must be explicit")


@dataclass(frozen=True)
class ActiveTrackingAllocation:
    """Proposed synthetic responses, not executed telemetry or a safety certificate."""

    requested_change_mw: float
    responses_mw: tuple[tuple[str, float], ...]
    # None means retain the existing command, including frozen resources.
    commands_mw: tuple[tuple[str, float | None], ...]
    unserved_change_mw: float
    version: Literal["active-tracking-allocation-v1"] = "active-tracking-allocation-v1"


class TrackingTransition(BaseModel):
    """Samples are end-of-step observations labelled by interval start time."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    start_minute: float
    observed_until_minute: float
    target: float
    target_change: float | None
    allowed_response_minutes: float
    deadline_minute: float
    entered_band_after_minutes: float | None = None
    confirmed_after_minutes: float | None = None
    post_deadline_max_error: float | None = None
    post_deadline_samples: int = Field(ge=0)
    response_status: TrackingStatus
    steady_status: TrackingStatus
    reason: str


class DynamicTrackingAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["dynamic-pcc-tracking-v1"] = "dynamic-pcc-tracking-v1"
    unit: Literal["MW", "Mvar"]
    limit: float = Field(gt=0)
    numerical_tolerance: float = Field(gt=0)
    status: TrackingStatus
    response_status: TrackingStatus
    steady_status: TrackingStatus
    raw_max_error: float | None = None
    raw_max_error_time_minute: float | None = None
    rmse: float | None = None
    post_deadline_max_error: float | None = None
    longest_outside_band_minutes: float = Field(ge=0)
    invalid_samples: int = Field(ge=0)
    transitions: tuple[TrackingTransition, ...]


class DeviceCheckpoint(BaseModel):
    """State before the next SIL minute; contains no future plant observations."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    minute: int = Field(ge=0)
    storage_energy_mwh: float
    storage_power_mw: float
    reactive_power_mvar: dict[str, float] = Field(default_factory=dict)


class StopDeviceSession(Exception):
    """Stop at a minute boundary and return only the completed execution prefix."""


class DevicePlanUpdate(BaseModel):
    """Prospective minute commands; injections positive, PCC imports positive."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    start_minute: int = Field(ge=0)
    p_mw: tuple[float, ...]
    q_mvar: tuple[float, ...]
    resource_p_mw: dict[str, tuple[float, ...]]
    resource_q_mvar: dict[str, tuple[float, ...]]

    @model_validator(mode="after")
    def aligned(self) -> "DevicePlanUpdate":
        n = len(self.p_mw)
        if (
            not n
            or not self.resource_p_mw
            or self.resource_p_mw.keys() != self.resource_q_mvar.keys()
        ):
            raise ValueError("minute update requires matching resource identities")
        if any(
            len(v) != n
            for v in (self.q_mvar, *self.resource_p_mw.values(), *self.resource_q_mvar.values())
        ):
            raise ValueError("minute update trajectories must align")
        return self


@dataclass(frozen=True)
class StoragePowerProjection:
    """Ordinary candidate after joint limits; signed unmet request is in MW."""

    actual_mw: float
    unserved_mw: float
    physical_override: bool

    def __post_init__(self) -> None:
        if not isfinite(self.actual_mw) or not isfinite(self.unserved_mw):
            raise ValueError("storage projection must be finite")


@dataclass(frozen=True)
class StorageDynamicsRecord:
    """One SIL interval, including ordinary intent and final protection outcome."""

    time_minutes: float
    previous_mw: float
    requested_mw: float
    ordinary_mw: float
    actual_mw: float
    maximum_change_mw: float
    ordinary_unserved_mw: float
    physical_override: bool
    hard_override: bool

    def __post_init__(self) -> None:
        values = (
            self.time_minutes,
            self.previous_mw,
            self.requested_mw,
            self.ordinary_mw,
            self.actual_mw,
            self.maximum_change_mw,
            self.ordinary_unserved_mw,
        )
        if any(isinstance(v, bool) or not isfinite(v) for v in values):
            raise ValueError("storage dynamics evidence must be finite")
        if self.time_minutes < 0 or self.maximum_change_mw < 0:
            raise ValueError("storage dynamics time and ramp allowance must be nonnegative")
        if type(self.physical_override) is not bool or type(self.hard_override) is not bool:
            raise ValueError("storage override flags must be boolean")

    @computed_field
    @property
    def ramp_compliant(self) -> bool:
        return abs(self.actual_mw - self.previous_mw) <= self.maximum_change_mw + 1e-9


@dataclass(frozen=True)
class StorageDynamicsTrajectory:
    """Missing historical evidence remains unassessed, never inferred as passed."""

    version: Literal["storage-dynamics-v1"] | None
    complete: bool
    records: tuple[StorageDynamicsRecord, ...]


@dataclass(frozen=True)
class WindActiveState:
    """Good-quality wind telemetry; measured MW is not an available-power bound."""

    resource_id: str
    measured_mw: float
    available_mw: float
    controllable: bool

    def __post_init__(self) -> None:
        if type(self.resource_id) is not str or not self.resource_id.strip():
            raise ValueError("wind resource identity is required")
        if type(self.controllable) is not bool:
            raise ValueError("wind controllability must be boolean")
        for value in (self.measured_mw, self.available_mw):
            if isinstance(value, bool) or not isfinite(value) or value < 0:
                raise ValueError("wind powers must be finite and nonnegative")


@dataclass(frozen=True)
class StorageActiveCapability:
    """Good-quality storage MW and permitted one-interval charge/discharge limits."""

    measured_mw: float
    controllable: bool
    charge_limit_mw: float
    discharge_limit_mw: float

    def __post_init__(self) -> None:
        if type(self.controllable) is not bool:
            raise ValueError("storage controllability must be boolean")
        if isinstance(self.measured_mw, bool) or not isfinite(self.measured_mw):
            raise ValueError("storage measured power must be finite")
        for value in (self.charge_limit_mw, self.discharge_limit_mw):
            if isinstance(value, bool) or not isfinite(value) or value < 0:
                raise ValueError("storage limits must be finite and nonnegative")
        if not self.controllable and (self.charge_limit_mw or self.discharge_limit_mw):
            raise ValueError("uncontrollable storage must have zero adjustable limits")


@dataclass(frozen=True)
class WindActiveSetpoint:
    resource_id: str
    target_mw: float | None

    def __post_init__(self) -> None:
        if type(self.resource_id) is not str or not self.resource_id.strip():
            raise ValueError("wind setpoint identity is required")
        if self.target_mw is not None and (
            isinstance(self.target_mw, bool) or not isfinite(self.target_mw) or self.target_mw < 0
        ):
            raise ValueError("wind target must be nonnegative finite MW or None")


@dataclass(frozen=True)
class WindStorageActivePlan:
    """Immutable proposed writes, not executed power; None means no write."""

    wind: tuple[WindActiveSetpoint, ...]
    storage_target_mw: float | None

    def __post_init__(self) -> None:
        if type(self.wind) is not tuple or not self.wind:
            raise ValueError("wind setpoints must be a nonempty immutable tuple")
        ids = [item.resource_id for item in self.wind]
        if len(set(ids)) != len(ids):
            raise ValueError("wind setpoint identities must be unique")
        if self.storage_target_mw is not None and (
            isinstance(self.storage_target_mw, bool) or not isfinite(self.storage_target_mw)
        ):
            raise ValueError("storage target must be finite MW or None")


@dataclass(frozen=True)
class MessageCursor:
    """Identity of accepted coordination state and its most recent send tick."""

    epoch: int
    sent_tick: int


@dataclass(frozen=True)
class ResourceActivePower:
    """Executed generation in MW, positive injection; never a target."""

    resource_id: str
    actual_mw: float


@dataclass(frozen=True)
class RestorationStation:
    """A synthetic/observed PV state; baseline is the unchanged upstream plan."""

    station_id: str
    actual_mw: float
    baseline_mw: float
    available_mw: float
    other_caps: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.station_id) is not str
            or not self.station_id.strip()
            or type(self.other_caps) is not tuple
        ):
            raise ValueError("restoration station identity and immutable caps are required")
        for value in (self.actual_mw, self.baseline_mw, self.available_mw):
            if isinstance(value, bool) or not isfinite(value) or value < 0:
                raise ValueError("restoration station powers must be finite and nonnegative")
        if any(type(pair) is not tuple or len(pair) != 2 for pair in self.other_caps):
            raise ValueError("other caps must be immutable owner/value pairs")
        owners = [owner for owner, _ in self.other_caps]
        if len(set(owners)) != len(owners) or any(not owner for owner in owners):
            raise ValueError("other cap owners must be nonempty and unique")
        if any(isinstance(cap, bool) or not isfinite(cap) or cap < 0 for _, cap in self.other_caps):
            raise ValueError("other caps must be finite and nonnegative")


@dataclass(frozen=True)
class RestorationObservation:
    time_minutes: float
    stations: tuple[RestorationStation, ...]
    valid: bool
    acknowledged_command_id: str | None

    def __post_init__(self) -> None:
        if (
            isinstance(self.time_minutes, bool)
            or not isfinite(self.time_minutes)
            or self.time_minutes < 0
        ):
            raise ValueError("observation time must be finite and nonnegative")
        if type(self.stations) is not tuple or not self.stations:
            raise ValueError("restoration observations require immutable station states")
        ids = [s.station_id for s in self.stations]
        if len(set(ids)) != len(ids) or type(self.valid) is not bool:
            raise ValueError("station identities must be unique and validity explicit")


@dataclass(frozen=True)
class RestorationCommand:
    command_id: str
    requested_mw: float
    cap_released_mw: float
    before: RestorationObservation

    def __post_init__(self) -> None:
        if (
            type(self.command_id) is not str
            or not self.command_id.strip()
            or isinstance(self.requested_mw, bool)
            or not isfinite(self.requested_mw)
            or self.requested_mw <= 0
        ):
            raise ValueError("restoration commands require identity and positive finite MW")
        if (
            isinstance(self.cap_released_mw, bool)
            or not isfinite(self.cap_released_mw)
            or self.cap_released_mw < 0
        ):
            raise ValueError("released cap must be finite and nonnegative")


@dataclass(frozen=True)
class RestorationEvidence:
    """Command-specific feedback, not a safety verdict or an ACK of execution."""

    command_id: str
    issued_at_minutes: float
    observed_at_minutes: float
    requested_mw: float
    cap_released_mw: float
    status: Literal["pending", "observed", "unknown"]
    measured_delta_mw: float | None
    achieved_mw: float | None
    reason: str
    version: Literal["restoration-evidence-v1"] = "restoration-evidence-v1"


@dataclass(frozen=True)
class ResourcePowerSeries:
    resource_id: str
    actual_mw: tuple[float, ...]


@dataclass(frozen=True)
class MinuteExecutionRecord:
    """Versioned sidecar projection; absent historical evidence stays absent."""

    version: Literal["minute-execution-v2"] | None
    time_minutes: tuple[float, ...]
    wind_resource_actual_mw: tuple[ResourcePowerSeries, ...]
    wind_availability_limited_mw: tuple[float, ...]
    pv_availability_limited_mw: tuple[float, ...]
    restoration_evidence: tuple[RestorationEvidence, ...]


class BusSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    bus_id: str = Field(min_length=1)
    values: tuple[float, ...]


class PlantInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: str = "synthetic-plant-v1"
    start: AwareDatetime
    step_minutes: int = Field(gt=0)
    load_p: tuple[BusSeries, ...]
    load_q: tuple[BusSeries, ...]
    wind_available: tuple[BusSeries, ...]
    pv_available: tuple[BusSeries, ...]

    @model_validator(mode="after")
    def validate_series(self) -> "PlantInputs":
        if self.schema_version != "synthetic-plant-v1":
            raise ValueError("unsupported plant input version")
        groups = (self.load_p, self.load_q, self.wind_available, self.pv_available)
        lengths = {len(item.values) for group in groups for item in group}
        if len(lengths) != 1 or 0 in lengths or not self.load_p:
            raise ValueError("plant series must share a nonzero length")
        for group in groups:
            if len({item.bus_id for item in group}) != len(group):
                raise ValueError("duplicate plant bus series")
            if any(not isfinite(v) for item in group for v in item.values):
                raise ValueError("plant values must be finite")
        if any(
            v < 0
            for group in (self.load_p, self.wind_available, self.pv_available)
            for item in group
            for v in item.values
        ):
            raise ValueError("gross load and available generation must be nonnegative")
        if {item.bus_id for item in self.load_p} != {item.bus_id for item in self.load_q}:
            raise ValueError("P/Q bus coverage differs")
        return self

    @property
    def steps(self) -> int:
        return len(self.load_p[0].values)

    def window(self, start: int, stop: int, at: datetime) -> "PlantInputs":
        if not 0 <= start < stop <= self.steps:
            raise ValueError("invalid plant input window")

        def cut(group: tuple[BusSeries, ...]) -> tuple[BusSeries, ...]:
            return tuple(
                BusSeries(bus_id=item.bus_id, values=item.values[start:stop]) for item in group
            )

        return PlantInputs(
            start=at,
            step_minutes=self.step_minutes,
            load_p=cut(self.load_p),
            load_q=cut(self.load_q),
            wind_available=cut(self.wind_available),
            pv_available=cut(self.pv_available),
        )

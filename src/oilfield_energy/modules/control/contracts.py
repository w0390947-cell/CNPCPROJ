"""Explicit synthetic plant inputs, separate from commands and observed output."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import ClassVar, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from .contracts_energy_policy import EnergyReturnPolicy as EnergyReturnPolicy
from .contracts_policy import BalancePolicy as BalancePolicy
from .contracts_policy import PolicyTransition as PolicyTransition
from .contracts_protection import ProtectionTarget as ProtectionTarget
from .contracts_protection import ProtectionTargetRecord as ProtectionTargetRecord

from .contracts_tube import (
    TubeBox as TubeBox,
)
from .contracts_tube import (
    TubeCommand as TubeCommand,
)
from .contracts_tube import (
    TubeDeviceModel as TubeDeviceModel,
)
from .contracts_tube import (
    TubeResource as TubeResource,
)
from .contracts_tube import (
    TubeState as TubeState,
)
from .contracts_tube import (
    TubeTransition as TubeTransition,
)


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
class ReactiveDynamicsRecord:
    """Ordinary Q dynamics evidence; no hardware-protection override is modeled.

    Capability reachability considers the command box, lag and shared ramp only;
    it does not certify arbitration or AC safety. See Reactive_Dynamics.md.
    """

    __pydantic_config__: ClassVar = ConfigDict(extra="forbid", allow_inf_nan=False)
    resource_ids: tuple[str, ...]
    q_initial_mvar: tuple[float, ...]
    q_targets_mvar: tuple[float, ...]
    q_actual_mvar: tuple[float, ...]
    q_minimum_mvar: tuple[float, ...]
    q_maximum_mvar: tuple[float, ...]
    alpha: float
    aggregate_ramp_mvar: float
    response_applied: bool
    aggregate_movement_mvar: float
    minimum_recovery_movement_mvar: float
    initial_capacity_within_limits: bool
    actual_capacity_within_limits: bool
    capability_reachable_without_arbitration: bool
    ramp_compliant: bool
    response_matches_prediction: bool
    version: Literal["reactive-dynamics-v1"] = "reactive-dynamics-v1"


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

# Strict improvement comparison in MW, not an engineering tracking deadband.
STORAGE_TRACKING_IMPROVEMENT_MW = 1e-9
StorageTrackingStatus = Literal[
    "adopted",
    "network_rejected",
    "not_improved",
    "baseline_unsafe",
    "blocked",
    "unchanged",
    "physical_override",
]


@dataclass(frozen=True)
class StorageTrackingPoint:
    """Read-only AC assessment at fixed load, other P/Q and cycle-start state."""

    power_mw: float
    error_mw: float | None
    safe: bool

    def __post_init__(self) -> None:
        values = (
            (self.power_mw,)
            if self.error_mw is None
            else (self.power_mw, self.error_mw)
        )
        if any(isinstance(v, bool) or not isfinite(v) for v in values):
            raise ValueError("storage tracking evidence must be finite")
        if type(self.safe) is not bool or (self.safe and self.error_mw is None):
            raise ValueError("safe storage tracking requires a valid AC error")


@dataclass(frozen=True)
class StorageTrackingResult:
    """Ordinary adoption evidence; subsequent hard protection remains independent."""

    baseline: StorageTrackingPoint
    candidate: StorageTrackingPoint | None
    proposed_mw: float
    selected_mw: float
    status: StorageTrackingStatus
    version: Literal["storage-tracking-v1"] = "storage-tracking-v1"
    improvement_tolerance_mw: float = STORAGE_TRACKING_IMPROVEMENT_MW


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
        values = (
            self.previous_mw,
            self.baseline_mw,
            self.minimum_mw,
            self.maximum_mw,
            self.ramp_mw,
            self.alpha,
        )
        if any(isinstance(v, bool) or not isfinite(v) for v in values):
            raise ValueError("active tracking inputs must be finite")
        if (
            self.minimum_mw > self.maximum_mw
            or self.ramp_mw < 0
            or not 0 < self.alpha <= 1
        ):
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
    active_power_mw: dict[str, float] = Field(default_factory=dict)
    gross_load_p_mw: dict[str, float] = Field(default_factory=dict)
    gross_load_q_mvar: dict[str, float] = Field(default_factory=dict)


class StorageResponseSettings(BaseModel):
    """Interpret a plan's storage P as a next-step actual target, not a command."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["storage-held-response-v1"] = "storage-held-response-v1"
    execution_minutes: float = Field(gt=0)
    time_constant_minutes: float = Field(gt=0)
    ramp_mw_per_minute: float = Field(ge=0)
    power_tolerance_mw: float = Field(gt=0)


class StoragePlanResponse(BaseModel):
    """Separate a nominal reachable target from the final arbitrated response."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    settings: StorageResponseSettings
    target_mw: float
    effective_command_mw: float
    predicted_mw: float
    actual_mw: float
    reachable: bool
    followed: bool

    @model_validator(mode="after")
    def actual_evidence(self) -> "StoragePlanResponse":
        reachable = (
            abs(self.predicted_mw - self.target_mw) <= self.settings.power_tolerance_mw
        )
        followed = (
            reachable
            and abs(self.actual_mw - self.target_mw) <= self.settings.power_tolerance_mw
        )
        if self.reachable != reachable or self.followed != followed:
            raise ValueError(
                "storage plan response flags must match the recorded powers"
            )
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
    plan_response: StoragePlanResponse | None = None

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
            raise ValueError(
                "storage dynamics time and ramp allowance must be nonnegative"
            )
        if (
            self.plan_response is not None
            and self.plan_response.actual_mw != self.actual_mw
        ):
            raise ValueError(
                "storage plan response must describe the same actual power"
            )
        if (
            type(self.physical_override) is not bool
            or type(self.hard_override) is not bool
        ):
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
            isinstance(self.target_mw, bool)
            or not isfinite(self.target_mw)
            or self.target_mw < 0
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
            isinstance(self.storage_target_mw, bool)
            or not isfinite(self.storage_target_mw)
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
            raise ValueError(
                "restoration station identity and immutable caps are required"
            )
        for value in (self.actual_mw, self.baseline_mw, self.available_mw):
            if isinstance(value, bool) or not isfinite(value) or value < 0:
                raise ValueError(
                    "restoration station powers must be finite and nonnegative"
                )
        if any(type(pair) is not tuple or len(pair) != 2 for pair in self.other_caps):
            raise ValueError("other caps must be immutable owner/value pairs")
        owners = [owner for owner, _ in self.other_caps]
        if len(set(owners)) != len(owners) or any(not owner for owner in owners):
            raise ValueError("other cap owners must be nonempty and unique")
        if any(
            isinstance(cap, bool) or not isfinite(cap) or cap < 0
            for _, cap in self.other_caps
        ):
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
            raise ValueError(
                "restoration observations require immutable station states"
            )
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
            raise ValueError(
                "restoration commands require identity and positive finite MW"
            )
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
        if {item.bus_id for item in self.load_p} != {
            item.bus_id for item in self.load_q
        }:
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
                BusSeries(bus_id=item.bus_id, values=item.values[start:stop])
                for item in group
            )

        return PlantInputs(
            start=at,
            step_minutes=self.step_minutes,
            load_p=cut(self.load_p),
            load_q=cut(self.load_q),
            wind_available=cut(self.wind_available),
            pv_available=cut(self.pv_available),
        )


class TransitionValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class TransitionActuator(TransitionValue):
    actuator: str = Field(min_length=1)
    previous_mw: float
    proposed_mw: float
    # Only an ordinary decrease may be deferred, at most to the previous P.
    # Mandatory actions, storage and unavailable resources use proposed_mw.
    defer_to_mw: float

    @model_validator(mode="after")
    def valid_deferral(self) -> "TransitionActuator":
        if (
            not self.proposed_mw
            <= self.defer_to_mw
            <= max(self.previous_mw, self.proposed_mw)
        ):
            raise ValueError(
                "deferral must stay between the proposal and previous injection"
            )
        return self


class TransitionBranchAssumption(TransitionValue):
    branch_id: str = Field(min_length=1)
    r_pu: float = Field(ge=0)
    receiving_apparent_power_max_mva: float = Field(gt=0)


class TransitionNetworkAssumptions(TransitionValue):
    network_id: str = Field(min_length=1)
    operating_mode_id: str = Field(min_length=1)
    base_mva: float = Field(gt=0)
    minimum_voltage_pu: float = Field(gt=0)
    branches: tuple[TransitionBranchAssumption, ...]

    @model_validator(mode="after")
    def unique_branches(self) -> "TransitionNetworkAssumptions":
        if len({b.branch_id for b in self.branches}) != len(self.branches):
            raise ValueError("network loss envelope cannot duplicate a branch")
        return self


class RegionalTransitionProposal(TransitionValue):
    version: Literal["cluster-transition-box-v1"] = "cluster-transition-box-v1"
    region: str = Field(min_length=1)
    start_minute: int = Field(ge=0)
    duration_minutes: float = Field(gt=0)
    gross_load_upper_mw: float = Field(ge=0)
    loss_upper_mw: float = Field(ge=0)
    # Lower-bound allowance for numerical aggregate/device reconciliation,
    # never an extra permission to exceed the cluster capacity.
    injection_roundoff_mw: float = Field(default=0, ge=0)
    loss_basis: Literal["lossless", "thermal-voltage-envelope", "explicit-upper-bound"]
    network_assumptions: TransitionNetworkAssumptions | None = None
    actuators: tuple[TransitionActuator, ...]

    @model_validator(mode="after")
    def identities(self) -> "RegionalTransitionProposal":
        ids = [a.actuator for a in self.actuators]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError(
                "transition actuator identities must be nonempty and unique"
            )
        if self.loss_basis == "lossless" and self.loss_upper_mw != 0:
            raise ValueError("a lossless envelope cannot contain losses")
        if (self.loss_basis == "thermal-voltage-envelope") != (
            self.network_assumptions is not None
        ):
            raise ValueError(
                "thermal loss envelopes must retain their network assumptions"
            )
        return self


class TransitionSetpoint(TransitionValue):
    actuator: str
    p_mw: float


class RegionalTransitionSelection(TransitionValue):
    region: str
    start_minute: int = Field(ge=0)
    setpoints: tuple[TransitionSetpoint, ...]


class ClusterTransitionDecision(TransitionValue):
    version: Literal["cluster-transition-box-v1"] = "cluster-transition-box-v1"
    status: Literal["certified", "unproven"]
    limit_mw: float = Field(gt=0)
    requested_upper_mw: float
    selected_upper_mw: float | None = None
    proposals: tuple[RegionalTransitionProposal, ...]
    selections: tuple[RegionalTransitionSelection, ...] = ()
    reason: str

    @model_validator(mode="after")
    def certificate_shape(self) -> "ClusterTransitionDecision":
        if self.status == "unproven":
            if self.selections or self.selected_upper_mw is not None:
                raise ValueError("an unproven interval cannot authorize endpoints")
            return self
        if self.selected_upper_mw is None or self.selected_upper_mw > self.limit_mw:
            raise ValueError("a certificate requires a bound within the capacity limit")
        proposals = {p.region: p for p in self.proposals}
        if (
            not proposals
            or len(proposals) != len(self.proposals)
            or len(self.selections) != len(proposals)
            or {s.region for s in self.selections} != set(proposals)
        ):
            raise ValueError(
                "a certificate requires complete unique regional selections"
            )
        for selection in self.selections:
            proposal = proposals[selection.region]
            points = {s.actuator: s.p_mw for s in selection.setpoints}
            if (
                selection.start_minute != proposal.start_minute
                or len(points) != len(selection.setpoints)
                or set(points) != {a.actuator for a in proposal.actuators}
                or any(
                    not a.proposed_mw <= points[a.actuator] <= a.defer_to_mw
                    for a in proposal.actuators
                )
            ):
                raise ValueError(
                    "a certificate cannot exceed regional authority or change identity"
                )
        return self


class RegionalTransitionReady(TransitionValue):
    region: str
    start_minute: int = Field(ge=0)
    selection: RegionalTransitionSelection
    status: Literal["passed", "unknown"]
    pcc_import_mw: float | None = None
    reason: str = ""

    @model_validator(mode="after")
    def readiness(self) -> "RegionalTransitionReady":
        if (
            self.region != self.selection.region
            or self.start_minute != self.selection.start_minute
        ):
            raise ValueError("readiness must match the selected interval and region")
        if self.status == "passed" and self.pcc_import_mw is None:
            raise ValueError("passed readiness requires known PCC power")
        return self


class ClusterTransitionRecord(TransitionValue):
    decision: ClusterTransitionDecision
    ready: tuple[RegionalTransitionReady, ...] = ()
    committed: bool = False
    reason: str = ""

    @model_validator(mode="after")
    def committed_evidence(self) -> "ClusterTransitionRecord":
        if self.committed and (
            self.decision.status != "certified"
            or self.reason
            or len(self.ready) != len(self.decision.selections)
            or any(
                r.status != "passed" or r.selection != s
                for r, s in zip(self.ready, self.decision.selections, strict=True)
            )
        ):
            raise ValueError(
                "committed evidence requires every certified endpoint to be ready"
            )
        return self

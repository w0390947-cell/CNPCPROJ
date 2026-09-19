"""Typed, versioned recipe for the synthetic Shancheng study."""

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class NetworkScenarioSummaryMetadata(StrictModel):
    """Versioned archive metadata, separate from legacy calculation objects."""

    schema_version: Literal["network-scenarios-v2"]
    scope: Literal["fixed_injection_balanced_radial_static_screening"]
    inputs_valid: bool | None
    all_final_states_secure: bool
    all_immediate_states_secure: bool
    n_minus_one_coverage_complete: bool
    field_acceptance_certified: Literal[False]


class ScenarioCatalog(StrictModel):
    schema_version: Literal["SC-scenarios-v1"]
    seed: int = Field(ge=0)
    cases: tuple[
        Literal[
            "normal",
            "load_drop",
            "wind_trip",
            "bad_quality",
            "stale_telemetry",
            "voltage_violation",
            "nonconvergence",
        ],
        ...,
    ]
    fault_start_minute: int = Field(ge=0)
    fault_end_minute: int = Field(gt=0)
    rearm_minute: int = Field(gt=0)
    window_intervals: Literal[4]

    @model_validator(mode="after")
    def validate_scenarios(self) -> "ScenarioCatalog":
        if len(self.cases) != 7 or len(set(self.cases)) != 7:
            raise ValueError("scenario catalog must cover all seven distinct cases")
        if not self.fault_start_minute < self.fault_end_minute < self.rearm_minute < 60:
            raise ValueError("invalid fault/rearm timing")
        return self


class ResourceSpec(StrictModel):
    resource_id: str
    bus_id: str
    kind: Literal["wind", "pv", "storage", "svg"]
    p_max_mw: float = Field(ge=0)
    s_max_mva: float = Field(gt=0)
    q_max_mvar: float = Field(ge=0)


class LoadSpec(StrictModel):
    bus_id: str
    peak_mw: float = Field(gt=0)
    power_factor: float = Field(gt=0, le=1)


class StorageSpec(StrictModel):
    energy_mwh: float = Field(gt=0)
    minimum_mwh: float = Field(ge=0)
    initial_mwh: float = Field(gt=0)
    eta_charge: float = Field(gt=0, le=1)
    eta_discharge: float = Field(gt=0, le=1)


class StudySpec(StrictModel):
    schema_version: Literal["shancheng-simulation-v1"]
    dataset_id: str
    revision: str
    synthetic: Literal[True]
    seed: int = Field(ge=0)
    start: AwareDatetime
    intervals: int = Field(ge=4, le=96)
    interval_minutes: Literal[15]
    network: dict[str, JsonValue]
    resources: tuple[ResourceSpec, ...]
    loads: tuple[LoadSpec, ...]
    storage: StorageSpec
    pcc_min_mw: float = Field(gt=0)
    pcc_max_mw: float = Field(gt=0)
    pf_min: float = Field(gt=0, le=1)
    voltage_min_pu: float = Field(gt=0)
    voltage_max_pu: float = Field(gt=0)
    solver_seconds: float = Field(gt=0)
    rolling_horizon_intervals: int = Field(ge=1, le=16)

    @model_validator(mode="after")
    def validate_recipe(self) -> "StudySpec":
        known = set(self.bus_ids)
        if self.network.get("pcc_bus_id") not in known:
            raise ValueError("PCC bus missing")
        if any(r.bus_id not in known for r in self.resources) or any(
            load.bus_id not in known for load in self.loads
        ):
            raise ValueError("unknown resource/load bus")
        if self.pcc_min_mw >= self.pcc_max_mw or self.voltage_min_pu >= self.voltage_max_pu:
            raise ValueError("invalid study limits")
        if not self.storage.minimum_mwh < self.storage.initial_mwh <= self.storage.energy_mwh:
            raise ValueError("invalid storage initial state")
        if len({r.resource_id for r in self.resources}) != len(self.resources):
            raise ValueError("duplicate resource ID")
        for kind, count in (("wind", 2), ("storage", 1), ("svg", 1)):
            if sum(r.kind == kind for r in self.resources) != count:
                raise ValueError(f"study requires {count} {kind} resources")
        if not any(r.kind == "pv" for r in self.resources):
            raise ValueError("study requires PV")
        if len({r.bus_id for r in self.resources if r.kind in ("wind", "pv")}) != sum(
            r.kind in ("wind", "pv") for r in self.resources
        ):
            raise ValueError("legacy optimization requires separate wind/PV buses")
        if len({load.bus_id for load in self.loads}) != len(self.loads):
            raise ValueError("duplicate load bus")
        return self

    @property
    def bus_ids(self) -> tuple[str, ...]:
        buses = self.network.get("buses")
        if not isinstance(buses, list):
            raise ValueError("network buses required")
        result: list[str] = []
        for bus in buses:
            if not isinstance(bus, dict) or not isinstance(bus.get("bus_id"), str):
                raise ValueError("invalid network bus")
            result.append(str(bus["bus_id"]))
        if not result or len(set(result)) != len(result):
            raise ValueError("empty or duplicate network buses")
        return tuple(result)


class EventType(str, Enum):
    PV_SURGE = "pv_surge"
    WIND_SURGE = "wind_surge"
    LOAD_DROP = "load_drop"
    LOAD_SURGE = "load_surge"
    COMMUNICATION_PACKET_LOSS = "communication_packet_loss"
    COMMUNICATION_OUTAGE = "communication_outage"


class EventTimeAxis(str, Enum):
    CLOCK_MINUTE = "clock_minute"
    COORDINATION_ITERATION = "coordination_iteration"


class ScenarioEventRecord(BaseModel):
    """Stored event description; reading evidence does not authorize execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    event_id: str = Field(min_length=1, max_length=48)
    event_type: EventType
    target: Literal["SC", "YA_B", "YA_C"]
    time_axis: EventTimeAxis
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    magnitude: float = Field(default=1.0, gt=0.0, le=5.0)
    label: str = Field(default="场景事件", min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_interval_and_axis(self) -> "ScenarioEventRecord":
        if self.end <= self.start:
            raise ValueError("event end must be greater than start")
        if self.time_axis is EventTimeAxis.CLOCK_MINUTE and self.end > 1440.0:
            raise ValueError("clock-minute events must end within the 24-hour horizon")
        physical = {
            EventType.PV_SURGE,
            EventType.WIND_SURGE,
            EventType.LOAD_DROP,
            EventType.LOAD_SURGE,
        }
        if self.event_type in physical and self.time_axis is not EventTimeAxis.CLOCK_MINUTE:
            raise ValueError("physical events require clock_minute time axis")
        if (
            self.event_type
            in {
                EventType.COMMUNICATION_PACKET_LOSS,
                EventType.COMMUNICATION_OUTAGE,
            }
            and self.time_axis is not EventTimeAxis.COORDINATION_ITERATION
        ):
            raise ValueError("communication events require coordination_iteration time axis")
        if self.event_type is EventType.COMMUNICATION_PACKET_LOSS and self.magnitude > 1.0:
            raise ValueError("packet-loss magnitude is a probability and must not exceed 1")
        if (
            self.event_type in {EventType.PV_SURGE, EventType.WIND_SURGE, EventType.LOAD_SURGE}
            and self.magnitude < 1.0
        ):
            raise ValueError("surge event magnitude must be at least 1")
        if self.event_type is EventType.LOAD_DROP and self.magnitude > 1.0:
            raise ValueError("load-drop magnitude must not exceed 1")
        return self


class ScenarioEvent(ScenarioEventRecord):
    """Executable event with current iteration semantics."""

    @model_validator(mode="after")
    def validate_executable_ticks(self) -> "ScenarioEvent":
        if self.time_axis is EventTimeAxis.COORDINATION_ITERATION:
            if self.start < 1 or not self.start.is_integer() or not self.end.is_integer():
                raise ValueError("communication events require positive integer ticks")
        return self


@dataclass(frozen=True)
class CommunicationFaultWindow:
    """Inclusive iteration window; packet loss retains explicit global semantics."""

    event_id: str
    kind: Literal["outage", "packet_loss"]
    scope: Literal["region", "global"]
    target: str | None
    start: int
    end: int
    probability: float

    def __post_init__(self) -> None:
        if not self.event_id or any(
            type(tick) is not int or tick < 1 for tick in (self.start, self.end)
        ):
            raise ValueError("event ID and positive integer ticks required")
        if not isfinite(self.probability) or not 0 <= self.probability <= 1:
            raise ValueError("event probability must be finite and in [0, 1]")
        if self.end < self.start:
            raise ValueError("fault window end must not precede start")
        if self.kind not in ("outage", "packet_loss"):
            raise ValueError("unsupported communication fault kind")
        if self.kind == "outage":
            if self.scope != "region" or not self.target or self.probability != 1:
                raise ValueError("outage requires a region and probability one")
        elif self.scope != "global" or self.target is not None:
            raise ValueError("packet loss is global; target must be null")


@dataclass(frozen=True)
class CommunicationEventExecution:
    """Observed coverage, not a claim that one event alone caused each drop."""

    window: CommunicationFaultWindow
    status: Literal["executed", "partially_executed", "not_reached"]
    observed_ticks: tuple[int, ...]
    matched_messages: int
    dropped_while_active: int


@dataclass(frozen=True)
class CommunicationEventEffect:
    """None delegates a fault kind to the legacy default configuration."""

    active: tuple[CommunicationFaultWindow, ...]
    outage: bool | None
    loss_probability: float | None
    loss_window_active: bool

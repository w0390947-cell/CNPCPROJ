"""Explicit model and point-mapping inputs for initialization."""

from typing import Annotated, Literal

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

from .contracts_timing import BoundedTimingPolicy
from .contracts_uncertainty import ObservationUncertaintyPolicy
from .contracts_values import ArtifactReference, Name, Nonnegative, Positive, Value


class Device(Value):
    resource_id: Name
    bus_id: Name
    kind: Literal["wind", "pv", "storage", "svg"]
    p_min_mw: StrictFloat
    p_max_mw: StrictFloat
    q_min_mvar: StrictFloat
    q_max_mvar: StrictFloat
    s_max_mva: Positive
    q_abs_over_p_max: Nonnegative | None
    p_control_enabled: StrictBool
    q_control_enabled: StrictBool
    alpha_p: Annotated[StrictFloat, Field(gt=0, le=1)]
    p_change_limit_mw_per_cycle: Nonnegative
    energy_min_mwh: Nonnegative | None = None
    energy_max_mwh: Positive | None = None
    eta_charge: Annotated[StrictFloat, Field(gt=0, le=1)] | None = None
    eta_discharge: Annotated[StrictFloat, Field(gt=0, le=1)] | None = None
    provenance_ref: Name

    @model_validator(mode="after")
    def capabilities(self) -> "Device":
        if self.p_min_mw > self.p_max_mw or self.q_min_mvar > self.q_max_mvar:
            raise ValueError("device bounds reversed")
        if not self.p_control_enabled and (self.p_min_mw != 0 or self.p_max_mw != 0):
            raise ValueError("uncontrolled nonzero P is not supported by this model")
        if not self.q_control_enabled and (
            self.q_min_mvar != 0 or self.q_max_mvar != 0
        ):
            raise ValueError("uncontrolled nonzero Q is not supported by this model")
        if self.kind != "storage" and self.p_min_mw < 0:
            raise ValueError("renewables and SVG cannot consume active power")
        if self.kind == "svg" and (self.p_min_mw != 0 or self.p_max_mw != 0):
            raise ValueError("SVG active power must be zero")
        energy = (
            self.energy_min_mwh,
            self.energy_max_mwh,
            self.eta_charge,
            self.eta_discharge,
        )
        if self.kind == "storage":
            if any(x is None for x in energy) or self.alpha_p != 1:
                raise ValueError(
                    "storage requires energy/efficiency fields and alpha_p=1"
                )
            assert self.energy_min_mwh is not None and self.energy_max_mwh is not None
            if self.energy_min_mwh >= self.energy_max_mwh:
                raise ValueError("storage energy bounds reversed")
        elif any(x is not None for x in energy):
            raise ValueError("energy parameters only belong to storage")
        return self


class PointMapping(Value):
    mapping_id: Name
    point_id: Name
    entity_kind: Literal["bus_load", "resource", "pcc", "operating_state"]
    entity_id: Name
    quantity: Literal[
        "p",
        "q",
        "energy",
        "voltage",
        "online",
        "remote_enabled",
        "blocked",
        "p_setpoint",
        "q_setpoint",
    ]
    access: Literal["read", "write"]
    raw_unit: Name
    normalized_unit: Literal["MW", "Mvar", "MWh", "pu", "1"]
    engineering_multiplier: Positive
    direction_rule_ref: Literal["identity", "negate"]
    quality_rule_ref: Name
    timestamp_rule_ref: Name
    scope_ref: Name
    provenance_ref: Name


class LoadScope(Value):
    scope_ref: Name
    bus_id: Name
    members: Annotated[tuple[Name, ...], Field(min_length=1)]
    semantics: Literal["gross_load"]
    provenance_ref: Name


class ModelPackage(Value):
    network_model: ArtifactReference | dict[str, JsonValue]
    operating_mode_id: Name
    devices: Annotated[tuple[Device, ...], Field(min_length=1, max_length=128)]
    point_mapping: ArtifactReference | tuple[PointMapping, ...]
    load_scopes: Annotated[tuple[LoadScope, ...], Field(min_length=1, max_length=512)]
    model_revision: Name
    mapping_revision: Name
    provenance_ref: Name


class ControlPolicy(Value):
    observation_uncertainty: ObservationUncertaintyPolicy | None = None
    bounded_timing: BoundedTimingPolicy | None = None
    execution_step_seconds: Literal[60]
    planning_update_seconds: Annotated[StrictInt, Field(gt=0)]
    planning_horizon_seconds: Annotated[StrictInt, Field(gt=0, le=86400)]
    response_deadline_seconds: Annotated[StrictInt, Field(gt=0)]
    p_tracking_tolerance_mw: Positive
    q_tracking_tolerance_mvar: Positive
    pcc_model_tolerance_mw: Nonnegative
    pcc_model_tolerance_mvar: Nonnegative
    alpha_q: Annotated[StrictFloat, Field(gt=0, le=1)]
    q_change_limit_mvar_per_cycle: Nonnegative
    settling_resolution: Annotated[StrictFloat, Field(gt=0, le=0.001)]
    voltage_min_pu: Positive
    voltage_max_pu: Positive
    pcc_min_mw: Nonnegative
    pcc_max_mw: Positive
    pf_min: Annotated[StrictFloat, Field(gt=0, le=1)]
    telemetry_max_age_seconds: Positive
    snapshot_max_skew_seconds: Nonnegative
    future_clock_skew_seconds: Nonnegative
    compute_budget_seconds: Positive
    dispatch_lead_seconds: Nonnegative
    transport_timeout_seconds: Positive
    feedback_timeout_seconds: Positive
    model_error_policy_ref: Literal[
        "exact-normalized-snapshot-v1", "bounded-observation-v1"
    ]
    availability_method_ref: Name
    local_fallback_policy_ref: Name

    @model_validator(mode="after")
    def clock(self) -> "ControlPolicy":
        timing = self.bounded_timing
        uncertain = self.observation_uncertainty
        if bool(uncertain) != (self.model_error_policy_ref == "bounded-observation-v1"):
            raise ValueError("Observation uncertainty must be explicitly admitted")
        if uncertain:
            if timing is None or timing.profile != "bounded_uncertain_v1":
                raise ValueError(
                    "Uncertain observation needs the matching timed profile"
                )
            radius = uncertain.boundary_radius_seconds
            if (
                timing.apply_not_before_seconds - timing.clock_error_bound_seconds
                <= radius
                or any(
                    timing.apply_not_after_seconds
                    + timing.clock_error_bound_seconds
                    + r.settling_max_seconds
                    + radius
                    >= 60
                    for r in timing.resources
                )
            ):
                raise ValueError(
                    "Sampling uncertainty must stay inside the settled hold guard"
                )
        elif timing and timing.profile == "bounded_uncertain_v1":
            raise ValueError("Uncertain timed profile requires metrology limits")
        if timing and (
            self.feedback_timeout_seconds + self.compute_budget_seconds
            > timing.prepare_max_seconds
            or timing.snapshot_delivery_max_seconds > self.feedback_timeout_seconds
            or self.dispatch_lead_seconds != 0
        ):
            raise ValueError(
                "Feedback and compute budgets must leave a bounded next-cycle send window"
            )
        if any(
            x % 60
            for x in (
                self.planning_update_seconds,
                self.planning_horizon_seconds,
                self.response_deadline_seconds,
            )
        ):
            raise ValueError("time settings must be on the one-minute grid")
        if self.planning_horizon_seconds % self.planning_update_seconds:
            raise ValueError("planning horizon must contain complete update intervals")
        if (
            self.voltage_min_pu >= self.voltage_max_pu
            or self.pcc_min_mw >= self.pcc_max_mw
        ):
            raise ValueError("network limits reversed")
        return self

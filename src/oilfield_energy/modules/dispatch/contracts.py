"""Immutable MW/MWh/CNY values; no solver or transport dependencies."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

ACCOUNTING_VERSION = "dispatch-economics-v2"


@dataclass(frozen=True)
class DispatchCapabilities:
    """One dispatch request's device participation, not physical nameplate data.

    Disabled storage provides neither active nor reactive support, matching
    existing central solvers. Its initial energy is held fixed.
    """

    storage_enabled: bool = True
    version: Literal["dispatch-capabilities-v1"] = "dispatch-capabilities-v1"

    def __post_init__(self) -> None:
        if type(self.storage_enabled) is not bool:
            raise ValueError("storage_enabled must be boolean")


@dataclass(frozen=True)
class CostRates:
    curtailment_cny_per_mwh: float
    storage_throughput_cny_per_mwh: float
    loss_cny_per_mwh: float


@dataclass(frozen=True)
class RenewableAccounting:
    """Chronological interval MW. Availability is AFTER the rated-MW bound.

    Raw predictions remain diagnostics. Excess above rated availability is not
    controllable curtailment. Tuples own their values and cannot mutate callers.
    """

    raw_available_mw: tuple[float, ...]
    available_mw: tuple[float, ...]
    nameplate_excess_mw: tuple[float, ...]
    curtailed_mw: tuple[float, ...]


@dataclass(frozen=True)
class EconomicCost:
    import_cost_cny: float
    curtailment_cost_cny: float
    storage_degradation_cost_cny: float
    loss_cost_cny: float
    economic_cost_cny: float


@dataclass(frozen=True)
class ReferenceEconomics:
    """Aggregate reference accounting, explicitly NOT regional AC realization."""

    reference: EconomicCost
    centralized_economic_cost_cny: float
    surrogate_objective_cny: float
    gap_percent: float | None
    comparison_status: Literal["comparable", "not_converged", "nonpositive_baseline"]
    accounting_version: Literal["dispatch-economics-v2"] = "dispatch-economics-v2"
    scope: Literal["aggregate_reference_with_calibrated_losses"] = (
        "aggregate_reference_with_calibrated_losses"
    )
    realized_regional_economic_cost_cny: None = None
    realization_status: Literal["not_computed"] = "not_computed"
    formal_ten_percent_requirement_certified: Literal[False] = False


@dataclass(frozen=True)
class RegionalPlanSnapshot:
    """Owned immutable values of the local solve used in an ADMM update."""

    name: str
    p_grid_mw: tuple[float, ...]
    q_grid_mvar: tuple[float, ...]
    renewable_used_mw: tuple[float, ...]
    storage_charge_mw: tuple[float, ...]
    storage_discharge_mw: tuple[float, ...]
    storage_energy_mwh: tuple[float, ...]
    q_support_mvar: tuple[float, ...]
    local_objective_cny: float
    status: str
    signal_iteration: int
    used_fallback: bool
    economic_cost: EconomicCost | None
    renewable_accounting: RenewableAccounting | None


@dataclass(frozen=True)
class CoordinationResiduals:
    primal: float
    dual: float
    primal_tolerance: float
    dual_tolerance: float


@dataclass(frozen=True)
class CoordinationSnapshot:
    """One completed barrier update; row order is the order of `regions`.

    The local plans consumed epoch k, and the new references/duals are for k+1.
    `converged` certifies the aggregate convex problem only, never execution.
    """

    epoch: int
    communication_tick: int
    converged: bool
    capabilities: DispatchCapabilities
    regions: tuple[RegionalPlanSnapshot, ...]
    p_references_mw: tuple[tuple[float, ...], ...]
    q_references_mvar: tuple[tuple[float, ...], ...]
    previous_p_references_mw: tuple[tuple[float, ...], ...]
    previous_q_references_mvar: tuple[tuple[float, ...], ...]
    dual_p: tuple[tuple[float, ...], ...]
    dual_q: tuple[tuple[float, ...], ...]
    rho: float
    residuals: CoordinationResiduals
    version: Literal["coordination-snapshot-v1"] = "coordination-snapshot-v1"


class ScheduleValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


ReplayPowerSeries = Annotated[tuple[float, ...], Field(strict=False)]


class RegionalCoordinationTrace(ScheduleValue):
    """Coordinator's latest accepted proposal, never a device measurement.

    Positive P/Q mean PCC import. Missing proposals remain None. Fresh means
    eligible for this barrier epoch, including an unexpired buffered response.
    Outage and local fallback are independent observations, not inferred from P.
    """

    region: str = Field(min_length=1)
    response_epoch: int | None = Field(ge=0)
    response_sent_tick: int | None = Field(ge=1)
    fresh: bool
    outage: bool
    fallback: bool
    proposal_p_mw: ReplayPowerSeries | None
    proposal_q_mvar: ReplayPowerSeries | None
    reference_p_mw: ReplayPowerSeries
    reference_q_mvar: ReplayPowerSeries

    @model_validator(mode="after")
    def aligned(self) -> "RegionalCoordinationTrace":
        count = len(self.reference_p_mw)
        if not count or len(self.reference_q_mvar) != count:
            raise ValueError("reference P/Q horizons must align")
        evidence = (
            self.response_epoch,
            self.response_sent_tick,
            self.proposal_p_mw,
            self.proposal_q_mvar,
        )
        if any(value is None for value in evidence):
            if not all(value is None for value in evidence) or self.fresh:
                raise ValueError(
                    "missing proposal must have no response provenance or freshness"
                )
        elif any(
            len(values) != count
            for values in (
                self.proposal_p_mw,
                self.proposal_q_mvar,
            )
            if values is not None
        ):
            raise ValueError("proposal and reference horizons must align")
        return self


class CoordinationIterationTrace(ScheduleValue):
    """Lightweight immutable replay evidence for one communication tick.

    Epoch identifies the barrier being collected. References are AFTER this
    tick's projection (unchanged if global_updated is false). Time is a separate
    planning axis in hours, never a conversion of communication_tick.
    """

    version: Literal["coordination-trace-v1"] = "coordination-trace-v1"
    communication_tick: int = Field(ge=1)
    coordination_epoch: int = Field(ge=0)
    global_updated: bool
    time_hours: tuple[float, ...] = Field(strict=False)
    regions: tuple[RegionalCoordinationTrace, ...] = Field(strict=False)

    @model_validator(mode="after")
    def aligned(self) -> "CoordinationIterationTrace":
        if (
            not self.time_hours
            or any(
                right <= left
                for left, right in zip(self.time_hours, self.time_hours[1:])
            )
            or self.time_hours[0] < 0
        ):
            raise ValueError(
                "planning time must be nonnegative and strictly increasing"
            )
        names = [region.region for region in self.regions]
        if not names or len(set(names)) != len(names):
            raise ValueError("trace requires unique region IDs")
        for region in self.regions:
            if len(region.reference_p_mw) != len(self.time_hours):
                raise ValueError("regional horizons must match planning time")
            if (
                region.response_sent_tick is not None
                and region.response_sent_tick > self.communication_tick
            ):
                raise ValueError("response cannot come from a future tick")
            if (
                region.response_epoch is not None
                and region.response_epoch > self.coordination_epoch
            ):
                raise ValueError("response cannot come from a future epoch")
            if region.fresh and region.response_epoch != self.coordination_epoch:
                raise ValueError("fresh response must belong to the collected epoch")
        if self.global_updated and not all(region.fresh for region in self.regions):
            raise ValueError("barrier update requires all fresh regions")
        return self


class ResourceTrajectory(ScheduleValue):
    resource_id: str = Field(min_length=1)
    bus_id: str = Field(min_length=1)
    resource_type: Literal["wind", "pv", "storage", "svg"]
    active_power_mw: tuple[float, ...]
    reactive_power_mvar: tuple[float, ...]

    @model_validator(mode="after")
    def aligned(self) -> "ResourceTrajectory":
        if not self.active_power_mw or len(self.active_power_mw) != len(
            self.reactive_power_mvar
        ):
            raise ValueError("resource P/Q trajectories must have equal nonzero length")
        return self


class AdoptedOrigin(ScheduleValue):
    interval_index: int = Field(ge=0)
    window_start_interval: int = Field(ge=0)
    window_end_interval: int = Field(gt=0)
    source_slot: Literal[0] = 0

    @model_validator(mode="after")
    def first_slot(self) -> "AdoptedOrigin":
        if (
            self.interval_index != self.window_start_interval
            or self.window_end_interval <= self.interval_index
        ):
            raise ValueError("adopted first slot must belong to its source window")
        return self


class AdoptedSchedule(ScheduleValue):
    """PCC import positive; resource injection positive; no global solve claim."""

    schema_version: Literal["adopted-schedule-v1"] = "adopted-schedule-v1"
    microgrid_id: str = Field(min_length=1)
    start: AwareDatetime
    step_minutes: int = Field(gt=0)
    p_grid_mw: tuple[float, ...]
    q_grid_mvar: tuple[float, ...]
    wind_available_mw: tuple[float, ...]
    resource_schedules: tuple[ResourceTrajectory, ...]
    origins: tuple[AdoptedOrigin, ...]
    scope: Literal["adopted_commands_not_measurements"] = (
        "adopted_commands_not_measurements"
    )
    optimality_status: Literal["not_assessed_for_adopted_horizon"] = (
        "not_assessed_for_adopted_horizon"
    )
    objective_cny: None = None
    objective_status: Literal["not_computed_for_adopted_horizon"] = (
        "not_computed_for_adopted_horizon"
    )

    @model_validator(mode="after")
    def aligned(self) -> "AdoptedSchedule":
        count = len(self.p_grid_mw)
        if not count or any(
            len(v) != count
            for v in (self.q_grid_mvar, self.wind_available_mw, self.origins)
        ):
            raise ValueError("adopted PCC/availability/provenance horizons must align")
        ids = [s.resource_id for s in self.resource_schedules]
        if (
            not ids
            or len(ids) != len(set(ids))
            or any(len(s.active_power_mw) != count for s in self.resource_schedules)
        ):
            raise ValueError("adopted resources must be unique and horizon aligned")
        indices = [s.interval_index for s in self.origins]
        if indices != list(range(indices[0], indices[0] + count)):
            raise ValueError("adopted intervals must be chronological and contiguous")
        return self


class FirstStepDecision(ScheduleValue):
    origin: AdoptedOrigin
    p_grid_mw: float
    q_grid_mvar: float
    wind_available_mw: float
    resources: tuple[ResourceTrajectory, ...]

    @model_validator(mode="after")
    def one_slot(self) -> "FirstStepDecision":
        ids = [s.resource_id for s in self.resources]
        if (
            not ids
            or len(ids) != len(set(ids))
            or any(len(s.active_power_mw) != 1 for s in self.resources)
        ):
            raise ValueError(
                "first-step decision requires one slot per unique resource"
            )
        return self


class LegacyAdoptedScheduleRecord(ScheduleValue):
    """Historical unversioned archive; certificates retain unverified scope.

    Read-only compatibility type, deliberately not accepted for execution.
    No invented timestamps, origins or full-horizon objective are added.
    """

    success: bool
    status: int
    message: str
    objective_cny: float
    solver_objective_without_constants_cny: float
    mip_gap: float | None
    microgrids: dict[str, dict[str, JsonValue]]
    cluster: dict[str, JsonValue]
    model_size: dict[str, int]

"""Immutable MW/MWh/CNY values; no solver or transport dependencies."""

from dataclasses import dataclass
from math import isfinite
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


class ComputationQualityPolicy(BaseModel):
    """Bounded offline solve budgets, not physical tolerances or a global optimum claim."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["quality-first-v1"] = "quality-first-v1"
    solve_seconds: tuple[float, ...] = (180.0, 600.0, 1800.0)
    admm_iterations: tuple[int, ...] = (360, 720, 1000)
    relative_gap: float = Field(default=1e-4, gt=0, lt=1)

    @model_validator(mode="after")
    def bounded_budgets(self) -> "ComputationQualityPolicy":
        for values, cap in ((self.solve_seconds, 1800), (self.admm_iterations, 1000)):
            if (
                not values
                or len(values) > 3
                or any(not isfinite(v) or v <= 0 or v > cap for v in values)
                or any(a >= b for a, b in zip(values, values[1:]))
            ):
                raise ValueError(
                    "quality budgets must be positive, increasing and bounded"
                )
        return self


class OptimizationAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    budget_seconds: float = Field(gt=0)
    solver_status: str
    feasible: bool
    objective_cny: float | None = None
    relative_gap: float | None = Field(default=None, ge=0)


class OptimizationQuality(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    status: Literal["satisfied", "budget_exhausted", "infeasible", "stopped"]
    target_relative_gap: float = Field(gt=0)
    attempts: tuple[OptimizationAttempt, ...]
    selected_attempt: int = Field(ge=1)

    @model_validator(mode="after")
    def selected_evidence(self) -> "OptimizationQuality":
        if self.selected_attempt > len(self.attempts):
            raise ValueError("selected optimization attempt is missing")
        selected = self.attempts[self.selected_attempt - 1]
        if self.status == "satisfied" and (
            not selected.feasible
            or selected.relative_gap is None
            or selected.relative_gap > self.target_relative_gap
        ):
            raise ValueError(
                "quality requires a feasible solution with sufficient gap evidence"
            )
        return self


class CoordinationBudgetEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    iteration_budget: int = Field(ge=1)
    completed_iterations: int = Field(ge=1)
    converged: bool
    primal_residual: float = Field(ge=0)
    dual_residual: float = Field(ge=0)
    primal_tolerance: float = Field(ge=0)
    dual_tolerance: float = Field(ge=0)


class ComputationQualityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    policy: ComputationQualityPolicy
    reference_optimizations: dict[str, tuple[OptimizationQuality, ...]]
    reference_coordination: tuple[CoordinationBudgetEvidence, ...]


class ReactivePlanningPolicy(BaseModel):
    """Study reserve and transition envelope; never expands physical Q authority."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["reactive-planning-v1"] = "reactive-planning-v1"
    reserve_fraction: float = Field(default=0.10, ge=0, lt=1)
    transition_minutes: float = Field(default=5.0, gt=0)
    source: str = "软件无功规划假设（参数随结果记录）；待现场动态与预测误差校准"


@dataclass(frozen=True)
class ReactivePlanningRow:
    """a P(t) + b Q(t) + c Q(t-1) <= upper, for one stable resource ID."""

    resource_id: str
    time_index: int
    p_coefficient: float
    q_coefficient: float
    previous_q_coefficient: float
    upper: float

    def __post_init__(self) -> None:
        if (
            not self.resource_id
            or type(self.time_index) is not int
            or self.time_index < 0
            or any(
                not isfinite(v)
                for v in (
                    self.p_coefficient,
                    self.q_coefficient,
                    self.previous_q_coefficient,
                    self.upper,
                )
            )
            or (self.time_index == 0 and self.previous_q_coefficient != 0)
        ):
            raise ValueError("invalid or noncausal reactive planning row")


@dataclass(frozen=True)
class ReactivePlanningResource:
    """Physical linear facets a*P+b*Q<=upper, with optional observed initial Q."""

    resource_id: str
    facets: tuple[tuple[float, float, float], ...]
    initial_q_mvar: float | None
    controllable: bool

    def __post_init__(self) -> None:
        if (
            not self.resource_id
            or not self.facets
            or type(self.controllable) is not bool
            or any(
                len(facet) != 3 or any(not isfinite(v) for v in facet)
                for facet in self.facets
            )
            or (self.initial_q_mvar is not None and not isfinite(self.initial_q_mvar))
        ):
            raise ValueError("invalid reactive planning resource")


class StorageReservePolicy(BaseModel):
    """Explicit study assumptions, not measured forecast-error statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["storage-reserve-v1"] = "storage-reserve-v1"
    forecast_error_fraction: float = Field(default=0.02, ge=0, lt=1)
    minimum_error_mw: float = Field(default=0.05, ge=0)
    support_minutes: int = Field(default=15, ge=1)
    recovery_minutes: int = Field(default=15, ge=1)
    trigger_fraction: float = Field(default=0.5, gt=0, le=1)
    replan_cooldown_minutes: int = Field(default=5, ge=1)
    source: str = (
        "软件仿真备用假设（参数随结果记录）；待现场预测误差统计校准，非现场运行定值"
    )


@dataclass(frozen=True)
class StorageReservePlan:
    """Planning envelope only; physical energy limits remain available to feedback."""

    up_mw: tuple[float, ...]
    down_mw: tuple[float, ...]
    energy_floor_mwh: tuple[float, ...]
    energy_ceiling_mwh: tuple[float, ...]
    minimum_power_mw: tuple[float, ...]
    maximum_power_mw: tuple[float, ...]

    def __post_init__(self) -> None:
        n = len(self.up_mw)
        if (
            not n
            or any(
                len(v) != n
                for v in (
                    self.down_mw,
                    self.minimum_power_mw,
                    self.maximum_power_mw,
                )
            )
            or any(
                len(v) != n + 1
                for v in (
                    self.energy_floor_mwh,
                    self.energy_ceiling_mwh,
                )
            )
        ):
            raise ValueError("reserve trajectories must align with planning intervals")
        if any(
            not isfinite(x)
            for v in (
                self.up_mw,
                self.down_mw,
                self.energy_floor_mwh,
                self.energy_ceiling_mwh,
                self.minimum_power_mw,
                self.maximum_power_mw,
            )
            for x in v
        ):
            raise ValueError("reserve trajectories must be finite")
        if min(*self.up_mw, *self.down_mw) < 0 or any(
            lo > hi
            for lows, highs in (
                (self.energy_floor_mwh, self.energy_ceiling_mwh),
                (self.minimum_power_mw, self.maximum_power_mw),
            )
            for lo, hi in zip(lows, highs)
        ):
            raise ValueError("requested storage reserve exceeds physical capacity")


@dataclass(frozen=True)
class PCCTrackingLimits:
    """Per-interval PCC absolute errors; positive P/Q mean grid import.

    Solvers enforce p_mw/q_mvar, without adding numerical_tolerance. Independent
    checks allow only that separately recorded floating-point residual. See ADR 0021.
    """

    p_mw: float
    q_mvar: float
    numerical_tolerance: float

    def __post_init__(self) -> None:
        if any(
            not isfinite(v) or v <= 0
            for v in (self.p_mw, self.q_mvar, self.numerical_tolerance)
        ):
            raise ValueError(
                "PCC tracking limits and numerical tolerance must be finite and positive"
            )
        if self.numerical_tolerance >= min(self.p_mw, self.q_mvar):
            raise ValueError(
                "numerical tolerance must be smaller than the engineering limits"
            )

    def accepts_p(self, error_mw: float) -> bool:
        return (
            isfinite(error_mw) and 0 <= error_mw <= self.p_mw + self.numerical_tolerance
        )

    def accepts_q(self, error_mvar: float) -> bool:
        return (
            isfinite(error_mvar)
            and 0 <= error_mvar <= self.q_mvar + self.numerical_tolerance
        )


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
    realized_regional_economic_cost_cny: float | None = None
    realization_status: Literal["not_computed", "computed", "unavailable"] = (
        "not_computed"
    )
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

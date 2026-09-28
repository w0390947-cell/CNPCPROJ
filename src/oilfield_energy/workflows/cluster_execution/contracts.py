"""Versioned evidence; numerical adapters never decide the overall UI status."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oilfield_energy.modules.control.contracts import (
    DynamicTrackingAssessment,
    DynamicTrackingPolicy,
)
from oilfield_energy.modules.dispatch.contracts import (
    ComputationQualityPolicy,
    CoordinationBudgetEvidence,
    OptimizationQuality,
    PCCTrackingLimits,
    ReactivePlanningPolicy,
    StorageReservePolicy,
)
from oilfield_energy.modules.power_flow.contracts import ClusterImportAssessment

ExecutionStageName = Literal["day_ahead", "intraday", "minute"]
ExecutionStatus = Literal["passed", "violated", "unknown", "not_computed"]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RollingProgress(EvidenceModel):
    """Observed work, not an elapsed-time estimate or an engineering verdict.

    Windows are one-based. Minutes are offsets from the simulation start.
    Only a completed prepare/advance pair increments completed_windows.
    """

    current_window: int = Field(ge=1)
    completed_windows: int = Field(ge=0)
    total_windows: int = Field(ge=1)
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    total_minutes: int = Field(gt=0)
    phase: Literal["preparing", "executing", "completed", "stopped"]

    @model_validator(mode="after")
    def valid_observation(self) -> "RollingProgress":
        if not self.current_window <= self.total_windows:
            raise ValueError("current window exceeds total windows")
        expected = self.current_window - (self.phase != "completed")
        if self.completed_windows != expected:
            raise ValueError("only completed execution may advance the window count")
        if not self.start_minute < self.end_minute <= self.total_minutes:
            raise ValueError("window must be within the simulation range")
        return self


class ExecutionPolicy(EvidenceModel):
    """Offline study tolerances, NOT customer-approved operating setpoints."""

    p_tracking_tolerance_mw: float = Field(default=0.05, gt=0)
    q_tracking_tolerance_mvar: float = Field(default=0.05, gt=0)
    numerical_tolerance: float = Field(default=1e-6, gt=0)
    source: str = (
        "软件仿真策略：工程限值、数值容差与分钟动态规则随结果记录；非现场验收定值"
    )
    device_step_minutes: Literal[1] = 1
    random_seed: int = 20260901
    update_minutes: int = Field(default=15, ge=1)
    horizon_minutes: int = Field(default=240, ge=1)
    # Historical records omitted these fields and used serial, fresh models.
    regional_plan_workers: int = Field(default=1, ge=1, le=3)
    numerical_threads_per_worker: Literal[1] | None = None
    coordination_model_reuse: bool = False
    # Missing on historical records: never synthesize a dynamic certificate.
    dynamic_tracking: DynamicTrackingPolicy | None = None
    # Historical results do not acquire evidence for a controller they never ran.
    active_tracking_version: Literal["active-tracking-allocation-v1"] | None = None
    storage_reserve: StorageReservePolicy | None = None
    reactive_planning: ReactivePlanningPolicy | None = None
    reactive_tracking_version: Literal["reactive-tracking-v1"] | None = None
    computation_quality: ComputationQualityPolicy | None = None

    @property
    def tracking_limits(self) -> PCCTrackingLimits:
        return PCCTrackingLimits(
            self.p_tracking_tolerance_mw,
            self.q_tracking_tolerance_mvar,
            self.numerical_tolerance,
        )

    @model_validator(mode="after")
    def rolling_grid(self) -> "ExecutionPolicy":
        _ = (
            self.tracking_limits
        )  # Validate numerical and engineering tolerances together.
        if self.dynamic_tracking is not None and (
            self.dynamic_tracking.p_change_threshold_mw >= self.p_tracking_tolerance_mw
            or self.dynamic_tracking.q_change_threshold_mvar
            >= self.q_tracking_tolerance_mvar
        ):
            raise ValueError(
                "target change thresholds must be smaller than tracking limits"
            )
        if (
            self.horizon_minutes < self.update_minutes
            or self.horizon_minutes % self.update_minutes
        ):
            raise ValueError("rolling horizon must contain integral update intervals")
        return self


class ExecutionCheck(EvidenceModel):
    code: str
    label: str
    status: ExecutionStatus
    actual: float | None = None
    limit: float | None = None
    unit: str = ""
    reason: str = ""


class RegionalExecution(EvidenceModel):
    region: str
    status: ExecutionStatus
    reason: str = ""
    optimization_quality: tuple[OptimizationQuality, ...] = ()
    checks: tuple[ExecutionCheck, ...] = ()
    economic_cost_cny: float | None = None
    time_minutes: tuple[float, ...] = ()
    p_mw: tuple[float, ...] = ()
    q_mvar: tuple[float, ...] = ()
    p_target_mw: tuple[float, ...] = ()
    q_target_mvar: tuple[float, ...] = ()
    storage_power_mw: tuple[float, ...] = ()
    storage_energy_mwh: tuple[float, ...] = ()
    # Device ID -> per-time-point evidence (plan or simulated response per stage).
    resource_p_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    resource_q_mvar: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    dynamic_tracking: dict[Literal["p", "q"], DynamicTrackingAssessment] = Field(
        default_factory=lambda: dict[Literal["p", "q"], DynamicTrackingAssessment]()
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> "RegionalExecution":
        n = len(self.time_minutes)
        arrays = (
            self.p_mw,
            self.q_mvar,
            self.p_target_mw,
            self.q_target_mvar,
            self.storage_power_mw,
            *self.resource_p_mw.values(),
            *self.resource_q_mvar.values(),
        )
        if any(len(v) != n for v in arrays):
            raise ValueError("execution trajectories must share the same time axis")
        if self.storage_energy_mwh and len(self.storage_energy_mwh) not in (n, n + 1):
            raise ValueError("invalid energy trajectory length")
        if any(b <= a for a, b in zip(self.time_minutes, self.time_minutes[1:])):
            raise ValueError("execution time must be strictly increasing")
        if self.status == "passed" and (
            not n or not self.checks or any(c.status != "passed" for c in self.checks)
        ):
            raise ValueError("a pass requires complete successful evidence")
        return self


class ExecutionStage(EvidenceModel):
    stage: ExecutionStageName
    status: ExecutionStatus
    reason: str = ""
    regions: tuple[RegionalExecution, ...] = ()
    cluster_import: ClusterImportAssessment


class RollingUpdate(EvidenceModel):
    """Information available before adoption, plus post-execution energy evidence."""

    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    horizon_end_minute: int = Field(gt=0)
    status: ExecutionStatus
    adopted: bool = False
    reason: str = ""
    coordination_quality: tuple[CoordinationBudgetEvidence, ...] = ()
    optimization_quality: dict[str, tuple[OptimizationQuality, ...]] = Field(default_factory=dict)
    initial_energy_mwh: dict[str, float] = Field(default_factory=dict)
    initial_reactive_mvar: dict[str, dict[str, float]] = Field(default_factory=dict)
    terminal_target_mwh: dict[str, float] = Field(default_factory=dict)
    unreserved_terminal_target_mwh: dict[str, float] = Field(default_factory=dict)
    reserve_up_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    reserve_down_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    reserve_energy_floor_mwh: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    reserve_energy_ceiling_mwh: dict[str, tuple[float, ...]] = Field(
        default_factory=dict
    )
    actual_end_energy_mwh: dict[str, float] = Field(default_factory=dict)
    previous_p_target_mw: dict[str, float] = Field(default_factory=dict)
    previous_q_target_mvar: dict[str, float] = Field(default_factory=dict)
    previous_target_source: (
        Literal["day_ahead_admm", "preceding_rolling_admm"] | None
    ) = None
    p_target_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    q_target_mvar: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    adopted_p_mw: dict[str, float] = Field(default_factory=dict)
    adopted_q_mvar: dict[str, float] = Field(default_factory=dict)
    admm_iterations: int = Field(default=0, ge=0)
    regional_checks: dict[str, tuple[ExecutionCheck, ...]] = Field(default_factory=dict)
    forecast_basis: str = (
        "同版本日内预测按窗口读取；反馈仅使用已执行状态，不读取未来分钟实绩"
    )


class MinuteReserveObservation(EvidenceModel):
    """At a minute boundary, based only on the last completed device state."""

    minute: int = Field(ge=0)
    region: str
    energy_mwh: float
    storage_power_mw: float
    required_mw: float = Field(ge=0)
    available_up_mw: float = Field(ge=0)
    available_down_mw: float = Field(ge=0)
    deficient: bool
    action: Literal["monitor", "recovering", "replan"] = "monitor"


class ReserveTargetAdjustment(EvidenceModel):
    minute: int = Field(ge=0)
    trigger_regions: tuple[str, ...]
    original_p_mw: dict[str, tuple[float, ...]]
    original_q_mvar: dict[str, tuple[float, ...]]
    adopted_p_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    adopted_q_mvar: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    update: RollingUpdate

    @model_validator(mode="after")
    def prospective_targets(self) -> "ReserveTargetAdjustment":
        count = self.update.end_minute - self.minute
        regions = set(self.original_p_mw)
        if (
            self.minute != self.update.start_minute
            or count <= 0
            or not regions
            or not self.trigger_regions
            or not set(self.trigger_regions) <= regions
            or set(self.original_q_mvar) != regions
            or any(
                len(v) != count
                for v in (*self.original_p_mw.values(), *self.original_q_mvar.values())
            )
        ):
            raise ValueError(
                "original targets must cover exactly the prospective window"
            )
        if self.update.adopted:
            if (
                set(self.adopted_p_mw) != regions
                or set(self.adopted_q_mvar) != regions
                or any(
                    len(v) != count
                    for v in (
                        *self.adopted_p_mw.values(),
                        *self.adopted_q_mvar.values(),
                    )
                )
            ):
                raise ValueError(
                    "adopted targets must cover the same regions and minutes"
                )
        elif self.adopted_p_mw or self.adopted_q_mvar:
            raise ValueError("rejected plans cannot carry adopted targets")
        return self


class ClusterExecution(EvidenceModel):
    version: Literal["cluster-execution-v1"] = "cluster-execution-v1"
    status: ExecutionStatus
    storage_enabled: bool
    policy: ExecutionPolicy
    input_basis: str
    dataset_sha256: str | None = None
    stages: tuple[ExecutionStage, ...]
    scope: Literal["synthetic_execution"] = "synthetic_execution"
    rolling_updates: tuple[RollingUpdate, ...] = ()
    minute_reserves: tuple[MinuteReserveObservation, ...] = ()
    reserve_target_adjustments: tuple[ReserveTargetAdjustment, ...] = ()

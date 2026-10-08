"""Versioned evidence; numerical adapters never decide the overall UI status."""

from .contracts_safety import (
    ClusterSafetyPolicy as ClusterSafetyPolicy,
    ClusterSafetyDomain as ClusterSafetyDomain,
    ClusterRegionDomain as ClusterRegionDomain,
    ClusterForecast as ClusterForecast,
    ClusterSafetyEvidence as ClusterSafetyEvidence,
)

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oilfield_energy.modules.control.contracts import (
    ClusterTransitionRecord,
    DynamicTrackingAssessment,
    DynamicTrackingPolicy,
)
from oilfield_energy.modules.dispatch.contracts import (
    ACRecoveryEvidence,
    ACRecoveryPolicy,
    ComputationQualityPolicy,
    CoordinationBudgetEvidence,
    CoordinationNetworkCalibration,
    CoordinationRecoveryPolicy,
    JointNetworkEvidence,
    NetworkCandidatePolicy,
    NetworkCandidateSelection,
    NetworkCoordinationPolicy,
    OptimizationQuality,
    PCCTrackingLimits,
    ReactivePlanningPolicy,
    RegionalNetworkCandidate,
    StorageReachabilityEvidence,
    StorageReachabilityPolicy,
    StorageReservePolicy,
    ReserveDeliveryPolicy,
    ReserveRecoveryTask,
)
from oilfield_energy.modules.power_flow.contracts import (
    ClusterImportAssessment,
    ClusterPowerFactorAssessment,
    ClusterPowerFactorPolicy,
)

from .contracts_reserve import ReserveClusterCertificate as ReserveClusterCertificate
from .contracts_reserve import ReserveServiceInput as ReserveServiceInput
from .contracts_reserve import ReserveServiceCertificate as ReserveServiceCertificate
from .contracts_reserve import ReserveActivationCommand as ReserveActivationCommand
from .contracts_reserve import ReserveResourceBox as ReserveResourceBox

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
    active_tracking_version: (
        Literal["active-tracking-allocation-v1", "cluster-command-projection-v1"] | None
    ) = None
    device_capability_version: (
        Literal["device-capability-v2", "cluster-device-v1"] | None
    ) = None
    storage_reserve: StorageReservePolicy | None = None
    cluster_safety: ClusterSafetyPolicy | None = None
    reserve_delivery: ReserveDeliveryPolicy | None = None
    storage_reachability: StorageReachabilityPolicy | None = None
    reactive_planning: ReactivePlanningPolicy | None = None
    reactive_tracking_version: (
        Literal["reactive-tracking-v1", "cluster-command-projection-v1"] | None
    ) = None
    reactive_dynamics_version: (
        Literal["reactive-dynamics-v1", "cluster-device-v1"] | None
    ) = None
    single_execution_version: Literal["unified-single-v7"] | None = None
    computation_quality: ComputationQualityPolicy | None = None
    # Historical records have neither restoration nor authorization to adopt it.
    ac_recovery: ACRecoveryPolicy | None = None
    # Absent on historical records: do not invent recovery that never ran.
    coordination_recovery: CoordinationRecoveryPolicy | None = None
    # Explicit opt-in; old records never acquire a network candidate search.
    network_candidates: NetworkCandidatePolicy | None = None
    network_coordination: NetworkCoordinationPolicy | None = None
    # Historical records retain missing continuous-transition evidence.
    cluster_transition_version: (
        Literal["cluster-transition-box-v1", "cluster-continuation-v1"] | None
    ) = None
    # Missing in historical results: no retroactive plan PF certificate.
    cluster_power_factor: ClusterPowerFactorPolicy | None = None

    @field_validator("reactive_planning", mode="before")
    @classmethod
    def preserve_unversioned_reactive_history(cls, value: object) -> object:
        # New callers pass an explicitly constructed v2 policy. Old serialized
        # records may omit defaults; reading them must never invent a v2 proof.
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            if "version" not in record:
                return {**record, "version": "reactive-planning-v1"}
            return record
        return value

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
    solver_stop_reason: str | None = None
    optimization_quality: tuple[OptimizationQuality, ...] = ()
    ac_recovery: ACRecoveryEvidence | None = None
    adoption_basis: Literal["standard", "ac_feasible_recovery", "ac_optimized_recovery"] = "standard"
    checks: tuple[ExecutionCheck, ...] = ()
    economic_cost_cny: float | None = None
    time_minutes: tuple[float, ...] = ()
    p_mw: tuple[float, ...] = ()
    q_mvar: tuple[float, ...] = ()
    p_target_mw: tuple[float, ...] = ()
    q_target_mvar: tuple[float, ...] = ()
    storage_power_mw: tuple[float, ...] = ()
    storage_energy_mwh: tuple[float, ...] = ()
    storage_reachability: StorageReachabilityEvidence | None = None
    storage_reachability_windows: tuple[StorageReachabilityEvidence, ...] = ()
    # Device ID -> per-time-point evidence (plan or simulated response per stage).
    resource_p_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    resource_q_mvar: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    dynamic_tracking: dict[Literal["p", "q"], DynamicTrackingAssessment] = Field(
        default_factory=lambda: dict[Literal["p", "q"], DynamicTrackingAssessment]()
    )

    @property
    def adoption_checks_passed(self) -> bool:
        """Only explicitly authorized economic uncertainty is nonblocking."""
        return bool(self.checks) and all(
            c.status == "passed" or (
                c.code == "OPTIMIZATION_QUALITY" and c.status == "unknown"
                and self.adoption_basis == "ac_feasible_recovery"
                and self.ac_recovery is not None
                and self.ac_recovery.permits_feasible_adoption
            ) for c in self.checks
        )

    @property
    def executable_plan(self) -> bool:
        """One authority for the required regional adoption certificates."""
        required = {"DEVICE_PLAN", "AC_NETWORK", "P_TRACKING", "Q_TRACKING"}
        passed = {c.code for c in self.checks if c.status == "passed"}
        return (
            self.status == "passed"
            and required <= passed
            and self.adoption_checks_passed
        )

    @model_validator(mode="after")
    def validate_evidence(self) -> "RegionalExecution":
        if self.adoption_basis != "standard":
            if self.ac_recovery is None or self.ac_recovery.outcome != "certified":
                raise ValueError("restoration adoption requires its selected certificate")
            proven = self.ac_recovery.original_optimality == "proven"
            if (self.adoption_basis == "ac_optimized_recovery") != proven:
                raise ValueError("adoption basis must preserve economic optimality semantics")
            if not proven and not self.ac_recovery.permits_feasible_adoption:
                raise ValueError("feasible-only adoption was not authorized")
        n = len(self.time_minutes)
        witness = self.storage_reachability
        if witness is not None and (
            witness.p_mw != self.storage_power_mw
            or witness.energy_mwh != self.storage_energy_mwh
            or witness.inputs.steps != n
            or not n
            or witness.inputs.start_minute != self.time_minutes[0]
            or self.resource_p_mw.get(witness.inputs.resource_id) != witness.p_mw
            or any(
                abs(
                    minute
                    - witness.inputs.start_minute
                    - t * witness.inputs.planning_minutes
                )
                > 1e-8
                for t, minute in enumerate(self.time_minutes)
            )
            or not any(
                c.code == "STORAGE_REACHABILITY" and c.status == witness.status
                for c in self.checks
            )
        ):
            raise ValueError(
                "storage reachability must certify this exact regional plan"
            )
        if witness is None and any(
            c.code == "STORAGE_REACHABILITY" for c in self.checks
        ):
            raise ValueError("a storage reachability check requires its witness")
        windows = self.storage_reachability_windows
        summary = [c for c in self.checks if c.code == "STORAGE_REACHABILITY_WINDOWS"]
        if windows or summary:
            if witness is not None or len(windows) != n or not n or len(summary) != 1:
                raise ValueError(
                    "rolling reachability summary requires every adopted window"
                )
            if (
                len(self.storage_power_mw) != n
                or len(self.storage_energy_mwh) < n
                or any(
                    w.inputs.start_minute != self.time_minutes[t]
                    or w.p_mw[0] != self.storage_power_mw[t]
                    or w.energy_mwh[0] != self.storage_energy_mwh[t]
                    or w.inputs.resource_id not in self.resource_p_mw
                    or len(self.resource_p_mw[w.inputs.resource_id]) != n
                    or self.resource_p_mw[w.inputs.resource_id][t] != w.p_mw[0]
                    for t, w in enumerate(windows)
                )
            ):
                raise ValueError(
                    "rolling reachability witnesses must bind the adopted prefix"
                )
            if summary[0].status == "passed" and any(
                w.status != "passed" for w in windows
            ):
                raise ValueError(
                    "rolling reachability pass requires all window certificates"
                )
        arrays = (
            self.p_mw,
            self.q_mvar,
            self.p_target_mw,
            self.q_target_mvar,
            self.storage_power_mw,
            *self.resource_p_mw.values(),
            *self.resource_q_mvar.values(),
        )
        missing_economic_targets = (
            not self.p_target_mw
            and not self.q_target_mvar
            and any(c.code == "CLUSTER_INTERVAL_SAFETY" for c in self.checks)
            and any(
                c.code == "ECONOMIC_TRACKING" and c.status == "unknown"
                for c in self.checks
            )
        )
        if missing_economic_targets:
            arrays = (
                self.p_mw,
                self.q_mvar,
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
            not n or not self.adoption_checks_passed
        ):
            raise ValueError("a pass requires complete successful evidence")
        return self


class RegionalCalibrationFeedback(EvidenceModel):
    """Diagnostic witness only; it deliberately lacks target-tracking certificates."""

    reference_round: int = Field(ge=1)
    evidence: RegionalExecution
    calibration: CoordinationNetworkCalibration | None = None

    @model_validator(mode="after")
    def certified_calibration(self) -> "RegionalCalibrationFeedback":
        codes = {c.code for c in self.evidence.checks if c.status == "passed"}
        if {"P_TRACKING", "Q_TRACKING"} & codes:
            raise ValueError("diagnostic feedback must not claim target adoption")
        if self.calibration is not None and (
            self.evidence.status != "passed"
            or not {"DEVICE_PLAN", "AC_NETWORK"} <= codes
            or len(self.calibration.p_loss_mw) != len(self.evidence.time_minutes)
        ):
            raise ValueError(
                "calibration requires an aligned certified physical witness"
            )
        return self


RecoveryOutcome = Literal[
    "passed",
    "regional_rejected",
    "regional_unresolved",
    "coordination_failed",
    "joint_rejected",
    "power_factor_rejected",
    "error",
]


class CoordinationRecoveryRound(EvidenceModel):
    round: int = Field(ge=1)
    outcome: RecoveryOutcome
    reason: str = ""
    coordination_stop_reason: str | None = None
    admm_iterations: int = Field(default=0, ge=0)
    coordination_quality: tuple[CoordinationBudgetEvidence, ...] = ()
    p_target_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    q_target_mvar: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    regions: tuple[RegionalExecution, ...] = ()
    cluster_import: ClusterImportAssessment | None = None
    cluster_power_factor: ClusterPowerFactorAssessment | None = None
    power_factor_margin_mvar: tuple[float, ...] = ()
    import_tracking_margin_mw: float = Field(default=0, ge=0)
    calibration_used: dict[str, CoordinationNetworkCalibration] | None = None
    feedback: tuple[RegionalCalibrationFeedback, ...] = ()
    network_candidates_used: tuple[RegionalNetworkCandidate, ...] = ()
    candidate_selection: NetworkCandidateSelection | None = None
    reference_method: Literal[
        "aggregate-admm", "network-socp-admm", "joint-network"
    ] = "aggregate-admm"
    joint_network: JointNetworkEvidence | None = None

    @model_validator(mode="after")
    def complete_adoption(self) -> "CoordinationRecoveryRound":
        if self.reference_method == "joint-network":
            if (
                self.joint_network is None
                or self.admm_iterations
                or self.coordination_quality
            ):
                raise ValueError(
                    "joint restoration must carry its own evidence, not ADMM iterations"
                )
            if self.outcome == "passed" and self.joint_network.status != "certified":
                raise ValueError(
                    "joint restoration adoption requires certified physical evidence"
                )
            if self.joint_network.status == "certified" and (
                self.p_target_mw != self.joint_network.p_mw
                or self.q_target_mvar != self.joint_network.q_mvar
            ):
                raise ValueError(
                    "joint references must match the certified network witness"
                )
        elif self.joint_network is not None:
            raise ValueError("joint evidence requires the joint reference method")
        if self.outcome in {"passed", "power_factor_rejected"} and (
            not self.regions
            or any(not r.executable_plan for r in self.regions)
            or len({r.region for r in self.regions}) != len(self.regions)
            or {r.region for r in self.regions} != set(self.p_target_mw)
            or set(self.p_target_mw) != set(self.q_target_mvar)
            or self.cluster_import is None
            or self.cluster_import.status != "passed"
            or (
                self.outcome == "passed"
                and self.cluster_power_factor is not None
                and self.cluster_power_factor.status != "passed"
            )
            or any(
                r.time_minutes != self.regions[0].time_minutes
                or r.p_target_mw != self.p_target_mw[r.region]
                or r.q_target_mvar != self.q_target_mvar[r.region]
                for r in self.regions
            )
        ):
            raise ValueError("adoption requires all regional and joint certificates")
        if self.outcome == "power_factor_rejected" and (
            self.cluster_power_factor is None
            or self.cluster_power_factor.status != "violated"
            or not self.cluster_power_factor.points
            or any(
                p.p_mw <= 0 or p.status == "unknown"
                for p in self.cluster_power_factor.points
            )
            or tuple(p.time_minutes for p in self.cluster_power_factor.points)
            != self.regions[0].time_minutes
        ):
            raise ValueError(
                "PF recovery requires complete violated positive-import evidence"
            )
        return self


RecoveryStopReason = Literal[
    "accepted",
    "disabled",
    "budget_exhausted",
    "stagnated",
    "feedback_unavailable",
    "regional_unresolved",
    "coordination_failed",
    "joint_rejected",
    "power_factor_rejected",
    "error",
]


class CoordinationRecoveryReport(EvidenceModel):
    version: Literal[
        "coordination-recovery-v1",
        "coordination-recovery-v2",
        "coordination-recovery-v3",
    ] = "coordination-recovery-v1"
    policy: CoordinationRecoveryPolicy | None = None
    power_factor_policy: ClusterPowerFactorPolicy | None = None
    network_candidate_policy: NetworkCandidatePolicy | None = None
    network_coordination_policy: NetworkCoordinationPolicy | None = None
    accepted: bool
    stop_reason: RecoveryStopReason
    reason: str = ""
    rounds: tuple[CoordinationRecoveryRound, ...]

    @model_validator(mode="after")
    def adoption_evidence(self) -> "CoordinationRecoveryReport":
        if (
            not self.rounds
            or tuple(r.round for r in self.rounds)
            != tuple(range(1, len(self.rounds) + 1))
            or self.accepted != (self.stop_reason == "accepted")
            or self.accepted != (self.rounds[-1].outcome == "passed")
            or len(self.rounds)
            > 1
            + (self.policy.max_recoordinations if self.policy else 0)
            + (
                self.network_coordination_policy.max_joint_attempts
                if self.network_coordination_policy
                else 0
            )
        ):
            raise ValueError("invalid bounded recovery history")
        if self.version != (
            "coordination-recovery-v3"
            if self.network_coordination_policy is not None
            else "coordination-recovery-v2"
            if self.network_candidate_policy is not None
            else "coordination-recovery-v1"
        ):
            raise ValueError(
                "network candidate recovery v2 requires an explicit policy"
            )
        joint_rows = [r for r in self.rounds if r.joint_network is not None]
        if len(self.rounds) - len(joint_rows) > 1 + (
            self.policy.max_recoordinations if self.policy else 0
        ):
            raise ValueError("ordinary recovery exceeds its own budget")
        if joint_rows and any(
            r.joint_network is None for r in self.rounds[joint_rows[0].round - 1 :]
        ):
            raise ValueError("joint recovery must be the final recovery phase")
        if joint_rows and (
            self.network_coordination_policy is None
            or len(joint_rows) > self.network_coordination_policy.max_joint_attempts
        ):
            raise ValueError("joint recovery exceeds its explicit policy budget")
        for row in self.rounds:
            if row.joint_network is not None:
                if self.network_coordination_policy is None or (
                    row.joint_network.time_limit_seconds
                    != self.network_coordination_policy.joint_time_limit_seconds
                    or row.joint_network.solver_relative_gap
                    > self.network_coordination_policy.relative_gap
                ):
                    raise ValueError(
                        "joint solver settings differ from the recorded policy"
                    )
                if (
                    row.round < 2
                    or row.network_candidates_used
                    or row.candidate_selection is not None
                ):
                    raise ValueError(
                        "joint recovery needs a preceding reference and independent solve"
                    )
                previous = self.rounds[row.round - 2]
                if (
                    row.joint_network.source_p_mw != previous.p_target_mw
                    or row.joint_network.source_q_mvar != previous.q_target_mvar
                    or row.joint_network.pf_margin_mvar != row.power_factor_margin_mvar
                ):
                    raise ValueError(
                        "joint recovery source and margin must match recorded history"
                    )
            if (
                row.network_candidates_used or row.candidate_selection is not None
            ) and (self.network_candidate_policy is None):
                raise ValueError("candidate evidence requires an explicit policy")
            candidates = (
                *row.network_candidates_used,
                *(
                    row.candidate_selection.candidates
                    if row.candidate_selection is not None
                    else ()
                ),
            )
            for candidate in candidates:
                if candidate.source_round > row.round:
                    raise ValueError(
                        "network candidate cannot come from a future round"
                    )
                source = self.rounds[candidate.source_round - 1]
                matches = [
                    f for f in source.feedback if f.evidence.region == candidate.region
                ]
                if len(matches) != 1 or (
                    matches[0].reference_round != candidate.source_round
                    or matches[0].calibration != candidate.calibration
                    or matches[0].evidence.status != "passed"
                    or matches[0].evidence.time_minutes != candidate.time_minutes
                    or matches[0].evidence.p_mw != candidate.p_mw
                    or matches[0].evidence.q_mvar != candidate.q_mvar
                ):
                    raise ValueError(
                        "network candidate requires its recorded physical witness"
                    )
            if row.network_candidates_used:
                previous = (
                    self.rounds[row.round - 2].candidate_selection
                    if row.round > 1
                    else None
                )
                if (
                    previous is None
                    or previous.candidates != row.network_candidates_used
                ):
                    raise ValueError(
                        "candidate attempt requires the preceding joint selection"
                    )
                if row.calibration_used != {
                    c.region: c.calibration for c in row.network_candidates_used
                }:
                    raise ValueError(
                        "candidate attempt must use each witness's own calibration"
                    )
            if (
                row.candidate_selection is not None
                and self.network_candidate_policy is not None
            ):
                if (
                    row.candidate_selection.combinations_checked
                    > self.network_candidate_policy.max_combination_checks
                ):
                    raise ValueError(
                        "candidate search exceeds the recorded policy budget"
                    )
        if self.accepted and self.power_factor_policy is not None:
            final = self.rounds[-1]
            pf = final.cluster_power_factor
            if (
                pf is None
                or pf.status != "passed"
                or pf.policy != self.power_factor_policy
                or not pf.points
                or any(p.status != "passed" for p in pf.points)
                or tuple(p.time_minutes for p in pf.points)
                != final.regions[0].time_minutes
            ):
                raise ValueError(
                    "adoption requires a complete final plan power factor certificate"
                )
        return self


class ExecutionStage(EvidenceModel):
    stage: ExecutionStageName
    status: ExecutionStatus
    reason: str = ""
    regions: tuple[RegionalExecution, ...] = ()
    cluster_import: ClusterImportAssessment
    cluster_power_factor: ClusterPowerFactorAssessment | None = None


class RollingUpdate(EvidenceModel):
    """Information available before adoption, plus post-execution energy evidence."""

    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    horizon_end_minute: int = Field(gt=0)
    status: ExecutionStatus
    adopted: bool = False
    continuation_available: bool = False
    reserve_tasks: tuple[ReserveRecoveryTask, ...] = ()
    reserve_certificates: tuple[ReserveClusterCertificate, ...] = ()
    reserve_correction_round: int = Field(default=0, ge=0)
    reserve_import_margin_mw: float = Field(default=0.0, ge=0)
    reserve_attempt_certificates: tuple[ReserveClusterCertificate, ...] = ()
    reason: str = ""
    reference_recovery: CoordinationRecoveryReport | None = None
    coordination_quality: tuple[CoordinationBudgetEvidence, ...] = ()
    optimization_quality: dict[str, tuple[OptimizationQuality, ...]] = Field(
        default_factory=dict
    )
    ac_recovery: dict[str, ACRecoveryEvidence] = Field(default_factory=dict)
    initial_energy_mwh: dict[str, float] = Field(default_factory=dict)
    initial_storage_power_mw: dict[str, float] = Field(default_factory=dict)
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
    """At a minute boundary; missing model_version preserves historical meaning.

    V2 availability is a nonnegative increment from storage_power_mw to an
    H-hour constant target, not a dynamic or network deliverability certificate.
    """

    minute: int = Field(ge=0)
    region: str
    energy_mwh: float
    storage_power_mw: float
    required_mw: float = Field(ge=0)
    available_up_mw: float = Field(ge=0)
    available_down_mw: float = Field(ge=0)
    deficient: bool
    fully_adequate: bool | None = None
    delivery_status: Literal["certified", "unknown"] | None = None
    recovery_deadline_minute: int | None = None
    action: Literal["monitor", "recovering", "replan"] = "monitor"
    model_version: Literal["storage-reserve-observation-v2"] | None = None
    support_hours: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def support_for_versioned_observation(self) -> "MinuteReserveObservation":
        if self.model_version is not None and self.support_hours is None:
            raise ValueError("versioned reserve observation requires support duration")
        return self


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
    # Absent in historical evidence: do not infer an algorithm from its schema.
    algorithm_version: (
        Literal[
            "cluster-rolling-v1",
            "cluster-rolling-v2",
            "cluster-rolling-v3",
            "cluster-rolling-v4",
            "cluster-rolling-v5",
            "cluster-rolling-v6",
        ]
        | None
    ) = None
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
    reserve_recovery_history: tuple[ReserveRecoveryTask, ...] = ()
    reserve_observations: tuple[ReserveClusterCertificate, ...] = ()
    reserve_target_adjustments: tuple[ReserveTargetAdjustment, ...] = ()
    day_ahead_recovery: CoordinationRecoveryReport | None = None
    cluster_transitions: tuple[ClusterTransitionRecord, ...] = ()
    safety: ClusterSafetyEvidence | None = None

"""Project numerical engines onto the solver-independent recovery workflow."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np

from oilfield_energy.data import ProjectCase
from oilfield_energy.hierarchy_types import (
    ADMMResult,
    ClusterReference,
    PhysicalCoordinationReference,
)
from oilfield_energy.model import OptimizationResult
from oilfield_energy.modules.dispatch.api import (
    build_coordination_calibration,
    cluster_import_tracking_margin,
)
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    JointNetworkEvidence,
    RegionalNetworkCandidate,
)
from oilfield_energy.modules.power_flow.api import (
    assess_cluster_import,
    assess_cluster_power_factor,
)
from oilfield_energy.modules.power_flow.contracts import (
    ClusterPowerFactorPolicy,
    RegionalImport,
    RegionalPCCPower,
)
from oilfield_energy.workflows.cluster_execution.api import (
    RecoveryResult,
    RecoveryTrial,
    executable_regional_plan,
    run_reference_recovery,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryRound,
    ExecutionPolicy,
    RecoveryOutcome,
    RegionalCalibrationFeedback,
    RegionalExecution,
)

from .cluster_joint_recovery import solve_joint_reference

CalibrationInput = (
    Mapping[str, CoordinationNetworkCalibration | Mapping[str, np.ndarray]] | None
)
RegionalPlans = dict[str, tuple[RegionalExecution, OptimizationResult | None]]
RegionalSolver = Callable[[ProjectCase, ClusterReference], RegionalPlans]
RegionalDiagnostics = Callable[
    [ProjectCase, ClusterReference, int], tuple[RegionalCalibrationFeedback, ...]
]


@dataclass(frozen=True)
class RegionalCoordinationPlan:
    reference: ClusterReference
    plans: RegionalPlans


def solve_with_reference_recovery(
    *,
    case: ProjectCase,
    policy: ExecutionPolicy,
    calibration: CalibrationInput,
    coordinate: Callable[
        [Mapping[str, CoordinationNetworkCalibration] | None, tuple[float, ...]],
        ADMMResult,
    ],
    aggregate_pf_target: float,
    solve_regions: RegionalSolver,
    diagnose_regions: RegionalDiagnostics | None,
    initial_reference: ADMMResult | None = None,
    storage_enabled: bool = True,
    stage: Literal["day_ahead", "intraday"] = "intraday",
    coordinate_candidates: Callable[
        [
            Mapping[str, CoordinationNetworkCalibration],
            tuple[float, ...],
            tuple[RegionalNetworkCandidate, ...],
        ],
        ADMMResult,
    ]
    | None = None,
) -> RecoveryResult[RegionalCoordinationPlan]:
    """All callbacks close over this window; diagnostic outputs never become plans."""
    names = tuple(m.name for m in case.microgrids)
    times = tuple(float(t * 60) for t in case.time_hours)
    pf_policy = policy.cluster_power_factor or ClusterPowerFactorPolicy(
        aggregate_pf_target
    )
    if pf_policy.target != aggregate_pf_target:
        raise ValueError("coordinator and final plan PF targets differ")
    normalized = (
        None
        if calibration is None
        else {
            n: row
            if isinstance(row, CoordinationNetworkCalibration)
            else build_coordination_calibration(
                p_loss_mw=row["p_loss_mw"],
                q_loss_mvar=row["q_loss_mvar"],
            )
            for n, row in calibration.items()
        }
    )

    def attempt(
        current: Mapping[str, CoordinationNetworkCalibration] | None,
        number: int,
        margin: tuple[float, ...],
        candidates: tuple[RegionalNetworkCandidate, ...] = (),
    ) -> RecoveryTrial[RegionalCoordinationPlan]:
        if candidates:
            if coordinate_candidates is None or current is None:
                raise ValueError("network candidate coordination port is missing")
            reference = coordinate_candidates(current, margin, candidates)
        else:
            reference = (
                initial_reference
                if number == 1 and initial_reference is not None
                else coordinate(current, margin)
            )
        return realize(reference, number)

    def realize(
        reference: ClusterReference,
        number: int,
        joint: JointNetworkEvidence | None = None,
    ) -> RecoveryTrial[RegionalCoordinationPlan]:
        plans: RegionalPlans = {}
        aggregate_check = None
        pf_check = None
        outcome: RecoveryOutcome = "coordination_failed"
        reason = (
            f"ADMM 未收敛，停止采用后续目标：{reference.stop_reason}"
            if isinstance(reference, ADMMResult)
            else ""
        )
        if (isinstance(reference, ADMMResult) and reference.converged) or (
            isinstance(reference, PhysicalCoordinationReference)
            and joint is not None
            and joint.status == "certified"
        ):
            plans = solve_regions(case, reference)
            if set(plans) != set(names) or any(
                r.region != n for n, (r, _) in plans.items()
            ):
                raise ValueError(
                    "regional plans must cover exactly the expected regions"
                )
            failed = [
                r
                for r, p in plans.values()
                if p is None or not executable_regional_plan(r)
            ]
            if failed:

                def reference_related(row: RegionalExecution) -> bool:
                    if row.solver_stop_reason == "solver_infeasible":
                        return True
                    rejected = [c for c in row.checks if c.status != "passed"]
                    return bool(rejected) and all(
                        c.status == "violated"
                        and c.code in {"P_TRACKING", "Q_TRACKING", "AC_NETWORK"}
                        for c in rejected
                    )

                outcome = (
                    "regional_rejected"
                    if all(reference_related(r) for r in failed)
                    else "regional_unresolved"
                )
                reason = "; ".join(
                    f"{r.region} 设备计划未满足采用条件：{r.reason} "
                    + "; ".join(
                        f"{c.label}: {c.status} {c.reason}"
                        for c in r.checks
                        if c.status != "passed"
                    )
                    for r in failed
                )
            else:
                if any(r.time_minutes != times for r, _ in plans.values()):
                    raise ValueError("regional realization time axes differ")
                aggregate_check = assess_cluster_import(
                    stage=stage,
                    expected_regions=names,
                    trajectories=tuple(
                        RegionalImport(
                            r.region,
                            r.time_minutes,
                            r.p_mw,
                            (True,) * len(r.p_mw),
                        )
                        for r, _ in plans.values()
                    ),
                    limit_mw=case.cluster_import_limit_mw,
                    tolerance_mw=policy.numerical_tolerance,
                )
                pf_check = assess_cluster_power_factor(
                    stage=stage,
                    expected_regions=names,
                    trajectories=tuple(
                        RegionalPCCPower(
                            r.region,
                            r.time_minutes,
                            r.p_mw,
                            r.q_mvar,
                            (True,) * len(r.p_mw),
                        )
                        for r, _ in plans.values()
                    ),
                    policy=pf_policy,
                )
                if aggregate_check.status != "passed":
                    outcome, reason = (
                        "joint_rejected",
                        "区域计划汇总超过集群受电容量，停止采用",
                    )
                elif pf_check.status != "passed":
                    outcome = (
                        "power_factor_rejected"
                        if pf_check.status == "violated"
                        and all(
                            p.p_mw > 0 and p.status != "unknown"
                            for p in pf_check.points
                        )
                        else "joint_rejected"
                    )
                    reason = "最终区域计划汇总功率因数未满足采用条件"
                else:
                    outcome, reason = "passed", ""
        return RecoveryTrial(
            RegionalCoordinationPlan(reference, plans),
            CoordinationRecoveryRound(
                round=number,
                import_tracking_margin_mw=cluster_import_tracking_margin(
                    region_count=len(names), tracking=policy.tracking_limits
                )
                if policy.cluster_safety is not None
                else 0.0,
                outcome=outcome,
                reason=reason,
                coordination_stop_reason=reference.stop_reason
                if isinstance(reference, ADMMResult)
                else None,
                admm_iterations=len(reference.history),
                coordination_quality=reference.quality_budgets,
                p_target_mw={
                    n: tuple(map(float, v))
                    for n, v in reference.p_references_mw.items()
                },
                q_target_mvar={
                    n: tuple(map(float, v))
                    for n, v in reference.q_references_mvar.items()
                },
                regions=tuple(r for r, _ in plans.values()),
                cluster_import=aggregate_check,
                cluster_power_factor=pf_check,
                reference_method="joint-network"
                if joint is not None
                else "network-socp-admm"
                if isinstance(reference, ADMMResult)
                and reference.network_model == "branch-flow-socp-v1"
                else "aggregate-admm",
                joint_network=joint,
            ),
        )

    def diagnose(
        payload: RegionalCoordinationPlan, number: int
    ) -> tuple[RegionalCalibrationFeedback, ...]:
        if diagnose_regions is None:
            raise RuntimeError("regional diagnostic port is missing")
        return diagnose_regions(case, payload.reference, number)

    def attempt_joint(
        payload: RegionalCoordinationPlan, number: int, margin: tuple[float, ...]
    ) -> RecoveryTrial[RegionalCoordinationPlan]:
        reference, evidence = solve_joint_reference(
            case,
            payload.reference,
            policy,
            storage_enabled,
            aggregate_pf_target,
            margin,
        )
        if reference is not None:
            return realize(reference, number, evidence)
        return RecoveryTrial(
            payload,
            CoordinationRecoveryRound(
                round=number,
                outcome="regional_unresolved",
                reason=evidence.reason,
                p_target_mw=evidence.source_p_mw,
                q_target_mvar=evidence.source_q_mvar,
                reference_method="joint-network",
                joint_network=evidence,
            ),
        )

    return run_reference_recovery(
        names=names,
        time_minutes=times,
        initial_calibration=normalized,
        policy=policy.coordination_recovery,
        attempt=attempt,
        diagnose=diagnose if diagnose_regions is not None else None,
        power_factor_policy=pf_policy,
        tracking_limits=policy.tracking_limits,
        network_candidate_policy=policy.network_candidates,
        import_limit_mw=case.cluster_import_limit_mw,
        attempt_candidates=attempt if coordinate_candidates is not None else None,
        network_coordination_policy=policy.network_coordination,
        attempt_joint=attempt_joint
        if policy.network_coordination is not None
        else None,
    )

"""Run every stage automatically using injected numerical operations."""

from .safety_controller import ClusterSafetyController as ClusterSafetyController
from .safety_replay import replay_cluster_safety as replay_cluster_safety
from .safety_network import bind_cluster_domain as bind_cluster_domain

from collections.abc import Callable, Sequence

from oilfield_energy.modules.power_flow.api import (
    assess_cluster_import,
    assess_cluster_power_factor,
)
from oilfield_energy.modules.power_flow.contracts import (
    RegionalImport,
    RegionalPCCPower,
)

from .contracts import (
    ClusterExecution,
    ExecutionPolicy,
    ExecutionStage,
    ExecutionStageName,
    ExecutionStatus,
    RegionalExecution,
)
from .recovery import (
    RecoveryResult as RecoveryResult,
)
from .recovery import (
    RecoveryTrial as RecoveryTrial,
)
from .recovery import (
    run_reference_recovery as run_reference_recovery,
)
from .reserve import observe_storage_reserve as observe_storage_reserve
from .reserve_delivery import certify_reserve_service as certify_reserve_service
from .reserve_delivery import certify_cluster_reserve as certify_cluster_reserve
from .reserve_delivery import reserve_activation_command as reserve_activation_command
from .rolling import run_feedback_loop as run_feedback_loop


def combined_status(values: Sequence[ExecutionStatus]) -> ExecutionStatus:
    if "violated" in values:
        return "violated"
    if values and all(v == "passed" for v in values):
        return "passed"
    if values and all(v == "not_computed" for v in values):
        return "not_computed"
    return "unknown"


def executable_regional_plan(region: RegionalExecution) -> bool:
    """Required plan certificates must all exist and pass before adoption."""
    return region.executable_plan


def verify_execution(
    *,
    names: tuple[str, ...],
    limit_mw: float,
    converged: bool,
    storage_enabled: bool,
    policy: ExecutionPolicy,
    input_basis: str,
    dataset_sha256: str | None,
    run_stage: Callable[[ExecutionStageName], tuple[RegionalExecution, ...]],
    progress: Callable[[str], None],
) -> ClusterExecution:
    stages: list[ExecutionStage] = []
    labels: dict[ExecutionStageName, str] = {
        "day_ahead": "自动落实 ADMM 区域设备计划并执行独立 AC 校核",
        "intraday": "自动滚动更新日内计划并执行分钟级状态反馈",
        "minute": "汇总分钟级设备响应并复核集群约束",
    }
    for stage, label in labels.items():
        progress(label)
        regions = (
            run_stage(stage) if converged or policy.cluster_safety is not None else ()
        )
        if regions and (
            {r.region for r in regions} != set(names) or len(regions) != len(names)
        ):
            raise ValueError("execution evidence must cover each region exactly once")
        trajectories = tuple(
            RegionalImport(
                r.region,
                r.time_minutes,
                r.p_mw,
                (_physical_evidence_valid(r),) * len(r.p_mw),
            )
            for r in regions
            if r.p_mw
        )
        assessment = assess_cluster_import(
            stage=stage,
            expected_regions=names,
            trajectories=trajectories,
            limit_mw=limit_mw,
            computed=bool(trajectories),
        )
        statuses: list[ExecutionStatus] = [r.status for r in regions]
        statuses.append(assessment.status)
        pf_assessment = None
        if policy.cluster_power_factor is not None and (
            stage != "minute" or policy.cluster_safety is not None
        ):
            pf_assessment = assess_cluster_power_factor(
                stage=stage,
                expected_regions=names,
                trajectories=tuple(
                    RegionalPCCPower(
                        r.region,
                        r.time_minutes,
                        r.p_mw,
                        r.q_mvar,
                        (_physical_evidence_valid(r),) * len(r.p_mw),
                    )
                    for r in regions
                    if r.p_mw
                ),
                policy=policy.cluster_power_factor,
                computed=bool(trajectories),
            )
            statuses.append(pf_assessment.status)
        status = combined_status(statuses)
        stages.append(
            ExecutionStage(
                stage=stage,
                status=status,
                reason="ADMM 未收敛，未将其参考作为执行目标" if not converged else "",
                regions=regions,
                cluster_import=assessment,
                cluster_power_factor=pf_assessment,
            )
        )
    return ClusterExecution(
        status=combined_status([s.status for s in stages]),
        storage_enabled=storage_enabled,
        policy=policy,
        input_basis=input_basis,
        dataset_sha256=dataset_sha256,
        stages=tuple(stages),
    )


def _physical_evidence_valid(region: RegionalExecution) -> bool:
    continuous = [c for c in region.checks if c.code == "CLUSTER_INTERVAL_SAFETY"]
    if continuous:
        return all(c.status == "passed" for c in continuous)
    if region.adoption_basis == "ac_feasible_recovery" and region.adoption_checks_passed:
        return True
    return all(c.status != "unknown" for c in region.checks)

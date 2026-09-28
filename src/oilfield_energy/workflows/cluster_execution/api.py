"""Run every stage automatically using injected numerical operations."""

from collections.abc import Callable, Sequence

from oilfield_energy.modules.power_flow.api import assess_cluster_import
from oilfield_energy.modules.power_flow.contracts import RegionalImport

from .contracts import (
    ClusterExecution,
    ExecutionPolicy,
    ExecutionStage,
    ExecutionStageName,
    ExecutionStatus,
    RegionalExecution,
)
from .reserve import observe_storage_reserve as observe_storage_reserve
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
    required = {"DEVICE_PLAN", "AC_NETWORK", "P_TRACKING", "Q_TRACKING"}
    passed = {c.code for c in region.checks if c.status == "passed"}
    return (
        region.status == "passed"
        and required <= passed
        and all(c.status == "passed" for c in region.checks)
    )


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
        regions = run_stage(stage) if converged else ()
        if regions and ({r.region for r in regions} != set(names) or len(regions) != len(names)):
            raise ValueError("execution evidence must cover each region exactly once")
        trajectories = tuple(
            RegionalImport(
                r.region,
                r.time_minutes,
                r.p_mw,
                (all(c.status != "unknown" for c in r.checks),) * len(r.p_mw),
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
        status = combined_status(statuses)
        stages.append(
            ExecutionStage(
                stage=stage,
                status=status,
                reason="ADMM 未收敛，未将其参考作为执行目标" if not converged else "",
                regions=regions,
                cluster_import=assessment,
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

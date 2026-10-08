"""Bounded joint MISOCP/AC recovery on the owner's immutable planning window."""

from time import monotonic
from dataclasses import replace
from typing import Mapping, TypedDict, cast

import numpy as np

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.data import ProjectCase
from oilfield_energy.hierarchy_types import (
    ClusterReference,
    PhysicalCoordinationReference,
)
from oilfield_energy.modules.dispatch.contracts import (
    JointNetworkEvidence,
    JointNetworkTarget,
)
from oilfield_energy.modules.dispatch.api import cluster_import_tracking_margin
from oilfield_energy.planning_security import build_planning_security_trajectories
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


class _JointInput(TypedDict):
    time_limit_seconds: float
    source_p_mw: dict[str, tuple[float, ...]]
    source_q_mvar: dict[str, tuple[float, ...]]
    time_minutes: tuple[float, ...]
    pf_margin_mvar: tuple[float, ...]
    solver_relative_gap: float


def solve_joint_reference(
    case: ProjectCase,
    previous: ClusterReference,
    policy: ExecutionPolicy,
    storage_enabled: bool,
    pf_target: float,
    margin: tuple[float, ...],
) -> tuple[PhysicalCoordinationReference | None, JointNetworkEvidence]:
    """Search the network model, including storage modes, beyond the candidate pool.

    The output is a reference witness. Strict regional re-realization and joint
    adoption checks still belong to the calling recovery adapter/workflow.
    """
    settings = policy.network_coordination
    if settings is None:
        raise ValueError("joint recovery requires an explicit policy")
    if policy.cluster_safety is not None:
        case = replace(
            case,
            cluster_import_limit_mw=case.cluster_import_limit_mw
            - cluster_import_tracking_margin(
                region_count=len(case.microgrids), tracking=policy.tracking_limits
            ),
        )
    started = monotonic()
    source_p = {n: tuple(map(float, v)) for n, v in previous.p_references_mw.items()}
    source_q = {n: tuple(map(float, v)) for n, v in previous.q_references_mvar.items()}
    common = _JointInput(
        time_limit_seconds=settings.joint_time_limit_seconds,
        source_p_mw=source_p,
        source_q_mvar=source_q,
        time_minutes=tuple(float(t * 60) for t in case.time_hours),
        pf_margin_mvar=margin,
        solver_relative_gap=min(
            settings.relative_gap, policy.computation_quality.relative_gap
        )
        if policy.computation_quality
        else settings.relative_gap,
    )
    security = build_planning_security_trajectories(case, list(source_p))
    try:
        result = solve_case_ac_consistent(
            case,
            storage_enabled=storage_enabled,
            pcc_targets={
                n: {"p_mw": np.array(source_p[n]), "q_mvar": np.array(source_q[n])}
                for n in source_p
            },
            # The first restoration phase owns reference distance; the second
            # owns the original economic/network objective, without a hidden sum.
            p_tracking_penalty_cny_per_mw=0.0,
            q_tracking_penalty_cny_per_mvar=0.0,
            time_limit_seconds=settings.joint_time_limit_seconds,
            relative_gap=common["solver_relative_gap"],
            relax_and_certify_storage_binaries=False,
            recovery_policy=policy.ac_recovery,
            p_grid_security_floors_mw={
                n: security[n].effective_floor_mw for n in source_p
            },
            joint_network_target=JointNetworkTarget(
                pf_target=pf_target,
                pf_margin_mvar=margin,
                p_scale_mw=policy.tracking_limits.p_mw,
                q_scale_mvar=policy.tracking_limits.q_mvar,
                deviation_tolerance=settings.deviation_tolerance,
            ),
        )
    except (ValueError, RuntimeError, FloatingPointError) as exc:
        return None, JointNetworkEvidence(
            **common,
            elapsed_seconds=monotonic() - started,
            status="error",
            reason=str(exc),
        )
    record = cast(
        Mapping[str, object],
        result.optimization.cluster.get("joint_network_recovery", {}),
    )

    def finite_number(key: str) -> float | None:
        value = record.get(key)
        return (
            float(value)
            if isinstance(value, (int, float)) and np.isfinite(value)
            else None
        )

    can_offer = result.passed and (
        result.recovery is None
        or result.recovery.original_optimality == "proven"
        or result.recovery.permits_feasible_adoption
    )
    evidence = JointNetworkEvidence(
        **common,
        elapsed_seconds=monotonic() - started,
        status="certified"
        if can_offer
        else "model_infeasible"
        if result.stop_reason == "solver_infeasible"
        else "unresolved",
        reason="认证可行计划的原目标精度未证明，当前策略禁止采用"
        if result.passed and not can_offer
        else result.stop_reason,
        deviation=finite_number("deviation"),
        deviation_bound=finite_number("deviation_bound"),
        deviation_optimal=record.get("deviation_optimal") is True,
        ac_iterations=result.iterations,
        ac_recovery=result.recovery,
        storage_integrality_certified=result.optimization.cluster.get(
            "storage_integrality_certified"
        )
        is True,
        solver_status=result.optimization.message,
        p_mw={
            n: tuple(map(float, np.asarray(r["p_grid_mw"], dtype=float)))
            for n, r in result.optimization.microgrids.items()
        }
        if can_offer
        else {},
        q_mvar={
            n: tuple(map(float, np.asarray(r["q_grid_mvar"], dtype=float)))
            for n, r in result.optimization.microgrids.items()
        }
        if can_offer
        else {},
    )
    if not can_offer:
        return None, evidence
    return PhysicalCoordinationReference(
        p_references_mw={
            n: np.array(r["p_grid_mw"], dtype=float, copy=True)
            for n, r in result.optimization.microgrids.items()
        },
        q_references_mvar={
            n: np.array(r["q_grid_mvar"], dtype=float, copy=True)
            for n, r in result.optimization.microgrids.items()
        },
    ), evidence

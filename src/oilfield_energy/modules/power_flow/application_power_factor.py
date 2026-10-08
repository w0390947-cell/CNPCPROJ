"""Independent synchronous plan PF gate; see docs/modeling/Cluster_Power_Factor.md."""

from collections.abc import Sequence
from math import fsum, hypot, isfinite

from .contracts import (
    AssessmentStatus,
    ClusterPowerFactorAssessment,
    ClusterPowerFactorPoint,
    ClusterPowerFactorPolicy,
    ClusterStage,
    RegionalPCCPower,
)


def assess_cluster_power_factor(
    *,
    stage: ClusterStage,
    expected_regions: Sequence[str],
    trajectories: Sequence[RegionalPCCPower],
    policy: ClusterPowerFactorPolicy,
    computed: bool = True,
) -> ClusterPowerFactorAssessment:
    """Every slot must pass. Undefined zero flow is unknown, never PF = 1."""
    names = tuple(expected_regions)
    if not names or len(set(names)) != len(names) or any(not n.strip() for n in names):
        raise ValueError("expected region IDs must be unique and nonempty")
    if stage not in {"centralized_reference", "admm_reference", "day_ahead", "intraday", "minute"}:
        raise ValueError("unknown cluster assessment stage")

    def unavailable(status: AssessmentStatus, reason: str) -> ClusterPowerFactorAssessment:
        return ClusterPowerFactorAssessment(stage, policy, status, (), reason)

    if not computed:
        if trajectories:
            raise ValueError("uncomputed stage cannot carry trajectories")
        return unavailable("not_computed", "stage_not_executed")
    if len(trajectories) != len(names) or {r.name for r in trajectories} != set(names):
        return unavailable("unknown", "missing_or_duplicate_regions")
    times = trajectories[0].time_minutes
    if (
        not times
        or any(not isfinite(t) or t < 0 for t in times)
        or any(b <= a for a, b in zip(times, times[1:]))
    ):
        return unavailable("unknown", "invalid_time_axis")
    for row in trajectories:
        if row.time_minutes != times:
            return unavailable("unknown", "unsynchronized_time_axes")
        if any(len(v) != len(times) for v in (row.p_mw, row.q_mvar, row.valid)):
            return unavailable("unknown", "invalid_dimensions")
        if not all(v is True for v in row.valid) or not all(
            isfinite(v) for v in (*row.p_mw, *row.q_mvar)
        ):
            return unavailable("unknown", "invalid_operating_evidence")
    points: list[ClusterPowerFactorPoint] = []
    try:
        for k, time in enumerate(times):
            p = fsum(row.p_mw[k] for row in trajectories)
            q = fsum(row.q_mvar[k] for row in trajectories)
            if not isfinite(p) or not isfinite(q):
                return unavailable("unknown", "nonfinite_aggregate")
            scale = max(abs(p), abs(q))
            pf = abs(p / scale) / hypot(p / scale, q / scale) if scale else None
            status: AssessmentStatus
            if p < 0:
                status, reason = "violated", "reverse_import"
            elif pf is None:
                status, reason = "unknown", "undefined_zero_flow"
            elif p == 0 or pf + policy.numerical_tolerance < policy.target:
                status, reason = "violated", "power_factor_below_target"
            else:
                status, reason = "passed", "within_target"
            points.append(ClusterPowerFactorPoint(time, p, q, pf, status, reason))
    except OverflowError:
        return unavailable("unknown", "nonfinite_aggregate")
    statuses = {point.status for point in points}
    overall: AssessmentStatus = (
        "violated" if "violated" in statuses else "unknown" if "unknown" in statuses else "passed"
    )
    return ClusterPowerFactorAssessment(stage, policy, overall, tuple(points), overall)

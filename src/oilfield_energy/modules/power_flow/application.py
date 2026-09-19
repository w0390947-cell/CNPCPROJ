"""Pure assessment use cases. Unknown inputs never become zero or safe."""

from collections.abc import Sequence
from math import fsum, isfinite
from typing import Literal

from .contracts import (
    AssessmentStatus,
    ClusterImportAssessment,
    ClusterStage,
    ClusterValidation,
    RegionalImport,
)


def assess_cluster_import(
    *,
    stage: ClusterStage,
    expected_regions: Sequence[str],
    trajectories: Sequence[RegionalImport],
    limit_mw: float,
    tolerance_mw: float = 1e-5,
    computed: bool = True,
) -> ClusterImportAssessment:
    """Assess every synchronous point, refusing implicit reordering/resampling.

    Invalid limit/configuration raises ValueError. Missing, nonfinite, invalid
    or asynchronous operating evidence yields `unknown`, with nullable metrics.
    An uncomputed stage must be requested explicitly and contains no evidence.
    """
    if not isfinite(limit_mw) or limit_mw < 0:
        raise ValueError("cluster import limit must be finite and nonnegative MW")
    if not isfinite(tolerance_mw) or tolerance_mw < 0:
        raise ValueError("import tolerance must be finite and nonnegative MW")
    if stage not in {"centralized_reference", "admm_reference", "day_ahead", "intraday", "minute"}:
        raise ValueError("unknown cluster assessment stage")
    names = tuple(expected_regions)
    if not names or any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError("expected region IDs must be unique and nonempty")

    def unavailable(status: AssessmentStatus, reason: str) -> ClusterImportAssessment:
        return ClusterImportAssessment(stage, status, limit_mw, tolerance_mw, None, None, 0, reason)

    if not computed:
        if trajectories:
            raise ValueError("uncomputed stage cannot carry trajectories")
        return unavailable("not_computed", "stage_not_executed")
    if len(trajectories) != len(names) or {t.name for t in trajectories} != set(names):
        return unavailable("unknown", "missing_or_duplicate_regions")
    times = trajectories[0].time_minutes
    if (
        not times
        or any(not isfinite(t) or t < 0 for t in times)
        or any(b <= a for a, b in zip(times, times[1:]))
    ):
        return unavailable("unknown", "invalid_time_axis")
    for item in trajectories:
        if item.time_minutes != times:
            return unavailable("unknown", "unsynchronized_time_axes")
        if len(item.import_mw) != len(times) or len(item.valid) != len(times):
            return unavailable("unknown", "invalid_dimensions")
        if not all(v is True for v in item.valid) or not all(isfinite(p) for p in item.import_mw):
            return unavailable("unknown", "invalid_operating_evidence")
    try:
        aggregate = tuple(fsum(t.import_mw[k] for t in trajectories) for k in range(len(times)))
    except OverflowError:
        return unavailable("unknown", "nonfinite_aggregate")
    if not all(isfinite(p) for p in aggregate):
        return unavailable("unknown", "nonfinite_aggregate")
    violations = sum(p > limit_mw + tolerance_mw for p in aggregate)
    return ClusterImportAssessment(
        stage,
        "violated" if violations else "passed",
        limit_mw,
        tolerance_mw,
        max(aggregate),
        violations,
        len(times),
        "import_limit_exceeded" if violations else "within_limit",
    )


def summarize_cluster_validation(
    stages: Sequence[ClusterImportAssessment],
    *,
    scope: Literal["reference_only", "hierarchical_execution"],
) -> ClusterValidation:
    """Keep reference certification separate from all three execution stages."""
    if scope not in {"reference_only", "hierarchical_execution"}:
        raise ValueError("unknown cluster validation scope")
    by_stage = {item.stage: item for item in stages}
    if len(by_stage) != len(stages):
        raise ValueError("duplicate cluster assessment stage")

    def status(required: tuple[ClusterStage, ...]) -> AssessmentStatus:
        values = tuple(by_stage[s].status if s in by_stage else "not_computed" for s in required)
        if "violated" in values:
            return "violated"
        if all(v == "passed" for v in values):
            return "passed"
        if all(v == "not_computed" for v in values):
            return "not_computed"
        return "unknown"

    execution: tuple[ClusterStage, ...] = ("day_ahead", "intraday", "minute")
    references: tuple[ClusterStage, ...] = ("centralized_reference", "admm_reference")
    required = references if scope == "reference_only" else (*references, *execution)
    return ClusterValidation(scope, status(required), status(execution), tuple(stages))

"""Whole-trajectory candidate selection, independent of solver and AC engines.

See docs/modeling/Network_Candidate_Feedback.md. Only supplied points are
selected. A convex combination of network witnesses is never inferred safe.
"""

from collections.abc import Mapping
from itertools import islice, product
from math import acos, fsum, hypot, isfinite, prod, tan

from .contracts import (
    NetworkCandidatePolicy,
    NetworkCandidateSelection,
    PCCTrackingLimits,
    RegionalNetworkCandidate,
)


def select_network_candidates(
    *,
    candidates: tuple[RegionalNetworkCandidate, ...],
    names: tuple[str, ...],
    time_minutes: tuple[float, ...],
    p_reference_mw: Mapping[str, tuple[float, ...]],
    q_reference_mvar: Mapping[str, tuple[float, ...]],
    import_limit_mw: float,
    import_tolerance_mw: float,
    power_factor_target: float,
    power_factor_tolerance: float,
    power_factor_margin_mvar: tuple[float, ...],
    tracking: PCCTrackingLimits,
    policy: NetworkCandidatePolicy,
    excluded: frozenset[tuple[tuple[str, int], ...]] = frozenset(),
) -> NetworkCandidateSelection:
    """Choose a joint feasible point nearest the current reference in tolerance units.

    Stable region/round ordering and an explicit enumeration budget make the
    search bounded. Excluded combinations still spend enumeration budget. The
    caller must independently re-coordinate, realize and certify the result.
    """
    n = len(time_minutes)
    if (
        not names
        or len(set(names)) != len(names)
        or not n
        or set(p_reference_mw) != set(names)
        or set(q_reference_mvar) != set(names)
        or len(power_factor_margin_mvar) != n
        or not 0 < power_factor_target <= 1
        or import_limit_mw <= 0
        or min(import_tolerance_mw, power_factor_tolerance) < 0
        or any(
            not isfinite(v)
            for v in (
                import_limit_mw,
                import_tolerance_mw,
                power_factor_target,
                power_factor_tolerance,
                *time_minutes,
                *power_factor_margin_mvar,
                *(v for row in (*p_reference_mw.values(), *q_reference_mvar.values()) for v in row),
            )
        )
        or any(v < 0 for v in power_factor_margin_mvar)
        or any(len(row) != n for row in (*p_reference_mw.values(), *q_reference_mvar.values()))
        or any(c.region not in names or c.time_minutes != time_minutes for c in candidates)
        or len({(c.region, c.source_round) for c in candidates}) != len(candidates)
    ):
        raise ValueError("candidate selection requires complete aligned finite inputs")
    groups = tuple(
        tuple(sorted((c for c in candidates if c.region == name), key=lambda c: c.source_round))
        for name in sorted(names)
    )
    total = prod(len(group) for group in groups)
    checked = 0
    best: tuple[RegionalNetworkCandidate, ...] = ()
    best_score: float | None = None
    kappa = tan(acos(power_factor_target))
    for combination in islice(product(*groups), policy.max_combination_checks):
        checked += 1
        identity = tuple((c.region, c.source_round) for c in combination)
        if identity in excluded:
            continue
        joint = tuple(
            (fsum(c.p_mw[t] for c in combination), fsum(c.q_mvar[t] for c in combination))
            for t in range(n)
        )
        if any(
            not isfinite(p)
            or not isfinite(q)
            or p <= 0
            or p > import_limit_mw + import_tolerance_mw
            or p / hypot(p, q) + power_factor_tolerance < power_factor_target
            or (margin > 0 and abs(q) + margin > kappa * p)
            for (p, q), margin in zip(joint, power_factor_margin_mvar)
        ):
            continue
        score = fsum(
            abs(p - p_reference_mw[c.region][t]) / tracking.p_mw
            + abs(q - q_reference_mvar[c.region][t]) / tracking.q_mvar
            for c in combination
            for t, (p, q) in enumerate(zip(c.p_mw, c.q_mvar))
        )
        if not isfinite(score):
            raise ValueError("candidate reference distance overflowed")
        if best_score is None or score < best_score:
            best, best_score = combination, score
    complete = checked == total
    return NetworkCandidateSelection(
        status="selected" if best else "no_joint_candidate" if complete else "budget_exhausted",
        candidates=best,
        combinations_checked=checked,
        total_combinations=total,
        search_complete=complete,
        normalized_reference_distance=best_score,
    )

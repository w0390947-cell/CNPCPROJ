"""Sufficient reference margin for bounded regional PCC tracking errors."""

from math import acos, inf, isfinite, nextafter, tan
from numbers import Integral

from .contracts import PCCTrackingLimits


def cluster_power_factor_tracking_margin(
    *, target: float, region_count: int, tracking: PCCTrackingLimits
) -> float:
    """Mvar: E_Q + tan(acos(target)) E_P, rounded outward.

    If |Q_ref| + M <= k P_ref and each actual error lies in its tracking
    interval, the triangle inequality proves |Q_actual| <= k P_actual.
    This is sufficient, not necessary; re-solving may become infeasible.
    The final independent PF check remains authoritative with numerical solvers.
    """
    if isinstance(target, bool) or not isfinite(target) or not 0 < target <= 1:
        raise ValueError("power factor target must be in (0, 1]")
    if isinstance(region_count, bool) or not isinstance(region_count, Integral) or region_count < 1:
        raise ValueError("region count must be a positive integer")
    p_error = nextafter(tracking.p_mw + tracking.numerical_tolerance, inf)
    q_error = nextafter(tracking.q_mvar + tracking.numerical_tolerance, inf)
    margin = nextafter(
        region_count * nextafter(q_error + nextafter(tan(acos(target)) * p_error, inf), inf), inf
    )
    if not isfinite(margin):
        raise ValueError("nonfinite tracking margin")
    return margin


def tighten_cluster_power_factor_reference(
    *,
    target: float,
    region_count: int,
    tracking: PCCTrackingLimits,
    current_mvar: tuple[float, ...],
    realized_p_mw: tuple[float, ...],
    realized_q_mvar: tuple[float, ...],
    violated: tuple[bool, ...],
) -> tuple[float, ...]:
    """Monotone per-slot feedback, capped by the full tracking-box bound.

    An observed deficit is not a globally valid feasibility cut: changing the
    reference changes realization. Only a new independent joint pass permits
    adoption. The finite workflow budget handles lack of convergence.
    """
    bound = cluster_power_factor_tracking_margin(
        target=target, region_count=region_count, tracking=tracking
    )
    if not current_mvar or any(
        len(v) != len(current_mvar) for v in (realized_p_mw, realized_q_mvar, violated)
    ):
        raise ValueError("PF feedback must cover exactly the planning axis")
    if (
        any(not isfinite(v) or not 0 <= v <= bound for v in current_mvar)
        or any(not isfinite(v) or v <= 0 for v in realized_p_mw)
        or any(not isfinite(v) for v in realized_q_mvar)
        or any(type(v) is not bool for v in violated)
    ):
        raise ValueError("invalid PF margin or realized positive-import evidence")
    k = tan(acos(target))
    # A separate numerical allowance, not the .05 engineering tracking box.
    guard = nextafter(region_count * tracking.numerical_tolerance * (1 + k), inf)
    # A failed nonzero margin doubles on the next trial to avoid indefinitely
    # approaching the boundary from below; the full error-box bound caps it.
    return tuple(
        min(bound, nextafter(max(2 * m, m + max(0.0, abs(q) - k * p) + guard), inf)) if bad else m
        for m, p, q, bad in zip(current_mvar, realized_p_mw, realized_q_mvar, violated, strict=True)
    )

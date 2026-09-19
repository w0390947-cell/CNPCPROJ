"""Pure synthetic execution limits and conservation-preserving disaggregation."""

from collections.abc import Mapping
from math import fsum, isfinite

from .contracts import ResourceActivePower

POWER_ROUNDOFF_MW = 1e-9


def limit_synthetic_generation(requested_mw: float, available_mw: float, rated_mw: float) -> float:
    """Apply a physical ceiling to a simulated response, never to field telemetry.

    Availability can exceed nameplate as a raw forecast; executed power cannot.
    Only sub-nanowatt numerical negative requests are rounded to zero.
    """
    if any(isinstance(v, bool) or not isfinite(v) for v in (requested_mw, available_mw, rated_mw)):
        raise ValueError("generation powers must be finite real MW")
    if requested_mw < -POWER_ROUNDOFF_MW or available_mw < 0 or rated_mw < 0:
        raise ValueError("generation powers must be nonnegative")
    return min(max(0.0, requested_mw), available_mw, rated_mw)


def disaggregate_executed_generation(
    total_mw: float,
    reference_mw: Mapping[str, float],
    available_mw: Mapping[str, float],
) -> tuple[ResourceActivePower, ...]:
    """Preserve an executed total or reject it; never silently lose generation.

    Same resource IDs are required. Results follow reference insertion order.
    The 1e-9 MW allowance is arithmetic roundoff, not an engineering dead band.
    """
    ids = tuple(reference_mw)
    if set(ids) != set(available_mw) or any(not identity for identity in ids):
        raise ValueError("generation resource IDs must match")
    weights = [float(reference_mw[i]) for i in ids]
    upper = [float(available_mw[i]) for i in ids]
    if any(not isfinite(v) or v < 0 for v in weights + upper) or not isfinite(total_mw):
        raise ValueError("generation inputs must be finite and nonnegative")
    ceiling = fsum(upper)
    if total_mw < -POWER_ROUNDOFF_MW or total_mw > ceiling + POWER_ROUNDOFF_MW:
        raise ValueError("executed generation total exceeds physical bounds")
    requested = min(max(0.0, total_mw), ceiling)
    values = [0.0] * len(ids)
    remaining = requested
    active: list[int] = [i for i, cap in enumerate(upper) if cap > 0]
    while active and remaining > POWER_ROUNDOFF_MW / 100:
        weight_sum = fsum(weights[i] for i in active)
        shares = {
            i: remaining * (weights[i] / weight_sum if weight_sum > 0 else 1 / len(active))
            for i in active
        }
        saturated = [i for i in active if shares[i] >= upper[i] - values[i]]
        if not saturated:
            for i in active:
                values[i] += shares[i]
            break
        for i in saturated:
            remaining -= upper[i] - values[i]
            values[i] = upper[i]
        active = [i for i in active if i not in saturated]
    if abs(fsum(values) - total_mw) > POWER_ROUNDOFF_MW:
        raise ValueError("generation disaggregation failed conservation")
    return tuple(ResourceActivePower(identity, value) for identity, value in zip(ids, values))

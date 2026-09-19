"""Pure fixed-grid validation and ordinary storage transition projection."""

from math import isclose, isfinite

from .contracts import StoragePowerProjection


def execution_substeps(planning_minutes: float, device_minutes: float) -> int:
    """Reject unsupported grids; tolerance is 1e-8 minutes, not a resampling policy."""
    if any(
        isinstance(v, bool) or not isfinite(v) or v <= 0 for v in (planning_minutes, device_minutes)
    ):
        raise ValueError("planning and device intervals must be finite and positive")
    ratio = planning_minutes / device_minutes
    if not isfinite(ratio):
        raise ValueError("time grid ratio must be finite")
    count = round(ratio)
    if count < 1 or not isclose(count * device_minutes, planning_minutes, rel_tol=0, abs_tol=1e-8):
        raise ValueError(
            "device step must exactly divide the optimization interval; resampling is unsupported"
        )
    return count


def constrain_storage_power(
    *,
    previous_mw: float,
    requested_mw: float,
    minimum_mw: float,
    maximum_mw: float,
    maximum_change_mw: float,
) -> StoragePowerProjection:
    """Project once onto physical and ramp bounds; report an empty intersection.

    Physical bounds are the caller's current full-interval PCS/energy envelope.
    If the intersection is empty, move to the physical endpoint nearest the
    previous actual power. Never sacrifice a physical limit to claim ramp success.
    """
    values = (previous_mw, requested_mw, minimum_mw, maximum_mw, maximum_change_mw)
    if any(isinstance(v, bool) or not isfinite(v) for v in values):
        raise ValueError("storage transition values must be finite real numbers")
    if minimum_mw > maximum_mw or maximum_change_mw < 0:
        raise ValueError("storage envelope must be ordered and ramp allowance nonnegative")
    ramp_min, ramp_max = previous_mw - maximum_change_mw, previous_mw + maximum_change_mw
    if not isfinite(ramp_min) or not isfinite(ramp_max):
        raise ValueError("storage ramp envelope overflow")
    low, high = max(minimum_mw, ramp_min), min(maximum_mw, ramp_max)
    conflict = low > high
    actual = (
        min(max(previous_mw, minimum_mw), maximum_mw)
        if conflict
        else min(max(requested_mw, low), high)
    )
    unserved = requested_mw - actual
    if not isfinite(unserved):
        raise ValueError("storage unmet request overflow")
    return StoragePowerProjection(actual, unserved, conflict)

"""Bounded wind/storage dispatch, model wind-storage-dispatch-v2.

Only explicit, valid telemetry and already authorized storage capability enter
this pure rule. Quality, P/Q transactions and command lifecycle belong to the
caller. See docs/modeling/Shancheng_Device_Limits.md.
"""

from math import isfinite

from .contracts import (
    StorageActiveCapability,
    WindActiveSetpoint,
    WindActiveState,
    WindStorageActivePlan,
)

# Numerical comparison tolerance in MW; never an engineering control deadband.
_POWER_TOLERANCE_MW = 1e-9


def wind_storage_target_bounds(
    wind: tuple[WindActiveState, ...], storage: StorageActiveCapability
) -> tuple[float, float]:
    """Return absolute MW bounds; lower > upper means no nonnegative target exists.

    Uncontrollable measured injections remain fixed, even during a capability
    drop. Controllable wind uses current available power, never stale output.
    """
    if type(wind) is not tuple or not wind:
        raise ValueError("wind states must be a nonempty immutable tuple")
    ids = [item.resource_id for item in wind]
    if len(set(ids)) != len(ids):
        raise ValueError("wind resource identities must be unique")
    fixed = sum(item.measured_mw for item in wind if not item.controllable)
    fixed += storage.measured_mw if not storage.controllable else 0.0
    available = sum(item.available_mw for item in wind if item.controllable)
    lower = max(0.0, fixed - storage.charge_limit_mw)
    upper = fixed + available + storage.discharge_limit_mw
    if not isfinite(lower) or not isfinite(upper):
        raise ValueError("aggregate capability overflow")
    return lower, upper


def allocate_wind_storage_target(
    wind: tuple[WindActiveState, ...],
    storage: StorageActiveCapability,
    target_mw: float,
    *,
    deadband_mw: float = 0.0,
) -> WindStorageActivePlan | None:
    """Propose bounded writes in input-ID order, or None for an infeasible target.

    Reduce from a physically attainable baseline: absorb with storage first,
    then curtail wind proportionally. Increase wind before authorized storage
    discharge. A capability drop is mandatory, never hidden by a deadband.
    """
    for value in (target_mw, deadband_mw):
        if isinstance(value, bool) or not isfinite(value) or value < 0:
            raise ValueError("target and deadband must be finite nonnegative MW")
    lower, upper = wind_storage_target_bounds(wind, storage)
    if lower > upper or not lower - _POWER_TOLERANCE_MW <= target_mw <= upper + _POWER_TOLERANCE_MW:
        return None
    target = min(upper, max(lower, target_mw))
    baseline = tuple(
        min(item.measured_mw, item.available_mw) if item.controllable else item.measured_mw
        for item in wind
    )
    fixed_storage = storage.measured_mw if not storage.controllable else 0.0
    baseline_total = sum(baseline) + fixed_storage
    delta = target - baseline_total
    # A deadband cannot retain a baseline outside the allowed absolute range.
    action_deadband = deadband_mw if lower <= baseline_total <= upper else 0.0
    wind_targets = list(baseline)
    storage_target = 0.0 if storage.controllable else None
    if delta < -action_deadband:
        charge = min(-delta, storage.charge_limit_mw)
        remaining = -delta - charge
        reducible = sum(value for value, item in zip(baseline, wind) if item.controllable)
        if reducible > 0:
            fraction = min(1.0, remaining / reducible)
            wind_targets = [
                value * (1.0 - fraction) if item.controllable else value
                for value, item in zip(baseline, wind)
            ]
        storage_target = -charge if storage.controllable else None
    elif delta > action_deadband:
        headroom = tuple(
            item.available_mw - value if item.controllable else 0.0
            for item, value in zip(wind, baseline)
        )
        total_headroom = sum(headroom)
        increase = min(delta, total_headroom)
        if total_headroom > 0:
            wind_targets = [
                value + increase * room / total_headroom for value, room in zip(baseline, headroom)
            ]
        storage_target = (
            min(storage.discharge_limit_mw, max(0.0, delta - increase))
            if storage.controllable
            else None
        )
    # Also check conservation when the feasible lower bound required charging
    # but a configured deadband would otherwise leave a negative total.
    total = sum(wind_targets) + (fixed_storage if storage_target is None else storage_target)
    if (
        total < lower - _POWER_TOLERANCE_MW
        or abs(total - target) > deadband_mw + _POWER_TOLERANCE_MW
    ):
        return None
    return WindStorageActivePlan(
        wind=tuple(
            WindActiveSetpoint(item.resource_id, value if item.controllable else None)
            for item, value in zip(wind, wind_targets)
        ),
        storage_target_mw=storage_target,
    )

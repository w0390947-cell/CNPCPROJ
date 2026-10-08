"""Ordinary Q transitions from the unchanged cycle start, without I/O.

Model and reachability proof: docs/modeling/Reactive_Dynamics.md.
The command box is supplied by the resource capability owner. This module
neither clips measured state nor introduces an independent protection action.
"""

from math import fsum, isfinite

from .contracts import ReactiveDynamicsRecord

_Q_TOLERANCE = 1e-8  # Mvar, numerical capacity/response comparison only.
_RAMP_TOLERANCE = 1e-9  # Mvar, not an added engineering allowance.


def _finite(values: tuple[float, ...]) -> bool:
    return all(not isinstance(v, bool) and isfinite(v) for v in values)


def reactive_cycle_response(
    initial_mvar: tuple[float, ...],
    targets_mvar: tuple[float, ...],
    *,
    alpha: float,
    ramp_mvar: float,
) -> tuple[float, ...]:
    """Apply one shared lag/ramp response; targets do not replace the initial state."""
    if (
        not initial_mvar
        or len(initial_mvar) != len(targets_mvar)
        or not _finite((*initial_mvar, *targets_mvar, alpha, ramp_mvar))
        or not 0 < alpha <= 1
        or ramp_mvar < 0
    ):
        raise ValueError("invalid ordinary reactive response inputs")
    change = tuple(
        alpha * (target - initial) for initial, target in zip(initial_mvar, targets_mvar)
    )
    if not _finite(change):
        raise ValueError("reactive response change overflow")
    movement = fsum(abs(v) for v in change)
    scale = min(1.0, ramp_mvar / movement) if movement > 0 else 0.0
    response = tuple(initial + scale * delta for initial, delta in zip(initial_mvar, change))
    if not _finite(response):
        raise ValueError("reactive response overflow")
    return response


def assess_reactive_cycle(
    *,
    resource_ids: tuple[str, ...],
    initial_mvar: tuple[float, ...],
    targets_mvar: tuple[float, ...],
    actual_mvar: tuple[float, ...],
    minimum_mvar: tuple[float, ...],
    maximum_mvar: tuple[float, ...],
    alpha: float,
    ramp_mvar: float,
    response_applied: bool,
) -> ReactiveDynamicsRecord:
    """Measure the entire cycle and assess box reachability independently of AC.

    Let d be the coordinatewise distance from q0 to the new capability box.
    Reaching that box requires sum(d) <= R and each outside coordinate's largest
    lagged move to cover d. Together these are sufficient before arbitration:
    u=q0+(projection(q0)-q0)/alpha reaches the nearest box point without ramp
    saturation. The projection is a mathematical witness, never executed state.
    """
    resource_ids = tuple(resource_ids)
    initial_mvar, targets_mvar, actual_mvar = (
        tuple(initial_mvar),
        tuple(targets_mvar),
        tuple(actual_mvar),
    )
    minimum_mvar, maximum_mvar = tuple(minimum_mvar), tuple(maximum_mvar)
    n = len(resource_ids)
    vectors = (initial_mvar, targets_mvar, actual_mvar, minimum_mvar, maximum_mvar)
    if (
        not n
        or len(set(resource_ids)) != n
        or any(not rid.strip() for rid in resource_ids)
        or any(len(v) != n or not _finite(v) for v in vectors)
        or any(low > high for low, high in zip(minimum_mvar, maximum_mvar))
        or type(response_applied) is not bool
    ):
        raise ValueError("invalid reactive cycle evidence")
    predicted = reactive_cycle_response(
        initial_mvar, targets_mvar, alpha=alpha, ramp_mvar=ramp_mvar
    )
    if not response_applied:
        predicted = initial_mvar
    distances = tuple(
        max(low - q, 0.0, q - high)
        for q, low, high in zip(initial_mvar, minimum_mvar, maximum_mvar)
    )
    required = fsum(distances)
    lag_reachable = all(
        (q <= high + _Q_TOLERANCE or q + alpha * (low - q) <= high + _Q_TOLERANCE)
        and (q >= low - _Q_TOLERANCE or q + alpha * (high - q) >= low - _Q_TOLERANCE)
        for q, low, high in zip(initial_mvar, minimum_mvar, maximum_mvar)
    )
    movement = fsum(abs(actual - initial) for actual, initial in zip(actual_mvar, initial_mvar))
    if not isfinite(required) or not isfinite(movement):
        raise ValueError("reactive cycle movement overflow")
    return ReactiveDynamicsRecord(
        resource_ids=resource_ids,
        q_initial_mvar=initial_mvar,
        q_targets_mvar=targets_mvar,
        q_actual_mvar=actual_mvar,
        q_minimum_mvar=minimum_mvar,
        q_maximum_mvar=maximum_mvar,
        alpha=alpha,
        aggregate_ramp_mvar=ramp_mvar,
        response_applied=response_applied,
        aggregate_movement_mvar=movement,
        minimum_recovery_movement_mvar=required,
        initial_capacity_within_limits=all(d <= _Q_TOLERANCE for d in distances),
        actual_capacity_within_limits=all(
            low - _Q_TOLERANCE <= q <= high + _Q_TOLERANCE
            for q, low, high in zip(actual_mvar, minimum_mvar, maximum_mvar)
        ),
        capability_reachable_without_arbitration=(
            required <= ramp_mvar + _RAMP_TOLERANCE and lag_reachable
        ),
        ramp_compliant=movement <= ramp_mvar + _RAMP_TOLERANCE,
        response_matches_prediction=all(
            abs(actual - expected) <= _Q_TOLERANCE
            for actual, expected in zip(actual_mvar, predicted)
        ),
    )

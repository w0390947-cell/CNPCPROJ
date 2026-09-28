"""CONTROL-Q-001: bounded PCC Q feedback through an injected safety predictor."""

from collections.abc import Callable
from math import isfinite

from .contracts import ReactiveTrackingPoint, ReactiveTrackingResult


def track_reactive_power(
    *,
    initial: ReactiveTrackingPoint,
    minimum_mvar: tuple[float, ...],
    maximum_mvar: tuple[float, ...],
    pcc_target_mvar: float,
    alpha: float,
    evaluate: Callable[[tuple[float, ...]], ReactiveTrackingPoint],
    maximum_iterations: int = 4,
) -> ReactiveTrackingResult:
    """Improve predicted PCC error without moving the start-of-cycle state.

    Increase capacitive injection for positive import error. Invert one lag
    step, distribute command change over remaining authorized headroom, and
    backtrack if the caller's projected AC response is unsafe or worse.
    Saturation and blocked authority remain visible as an unserved residual.
    """
    n = len(initial.controls)
    if (
        not n
        or len(minimum_mvar) != n
        or len(maximum_mvar) != n
        or not 0 < alpha <= 1
        or not isfinite(pcc_target_mvar)
        or type(maximum_iterations) is not int
        or not 1 <= maximum_iterations <= 20
        or any(
            not isfinite(v) for v in (*minimum_mvar, *maximum_mvar, *initial.controls)
        )
        or any(
            lo > hi or not lo - 1e-9 <= v <= hi + 1e-9
            for lo, hi, v in zip(minimum_mvar, maximum_mvar, initial.controls)
        )
    ):
        raise ValueError("invalid reactive tracking inputs")

    def valid(point: ReactiveTrackingPoint) -> bool:
        return (
            point.safe
            and point.pcc_q_mvar is not None
            and isfinite(point.pcc_q_mvar)
            and all(
                len(v) == n and all(isfinite(x) for x in v)
                for v in (point.controls, point.targets, point.responses)
            )
            and all(
                lo - 1e-9 <= value <= hi + 1e-9
                for values in (point.controls, point.targets, point.responses)
                for lo, hi, value in zip(minimum_mvar, maximum_mvar, values)
            )
        )

    initial_error = (
        None
        if initial.pcc_q_mvar is None or not isfinite(initial.pcc_q_mvar)
        else initial.pcc_q_mvar - pcc_target_mvar
    )
    if not valid(initial):
        return ReactiveTrackingResult(
            initial, initial_error, initial_error, 0, "blocked"
        )
    best, count = initial, 0
    for _ in range(maximum_iterations):
        assert best.pcc_q_mvar is not None
        error = best.pcc_q_mvar - pcc_target_mvar
        if abs(error) <= 1e-4:  # Action accuracy, not the engineering acceptance band.
            break
        direction = 1.0 if error > 0 else -1.0
        rooms = tuple(
            max(0.0, hi - v if direction > 0 else v - lo)
            for lo, hi, v in zip(minimum_mvar, maximum_mvar, best.controls)
        )
        room = sum(rooms)
        if room <= 1e-9:
            break
        movement = min(abs(error) / alpha, room)
        improved = False
        for fraction in (1.0, 0.5, 0.25, 0.125):
            commands = tuple(
                min(hi, max(lo, v + direction * movement * fraction * space / room))
                for lo, hi, v, space in zip(
                    minimum_mvar, maximum_mvar, best.controls, rooms
                )
            )
            candidate = evaluate(commands)
            count += 1
            if (
                valid(candidate)
                and candidate.pcc_q_mvar is not None
                and abs(candidate.pcc_q_mvar - pcc_target_mvar) < abs(error) - 1e-9
            ):
                best, improved = candidate, True
                break
        if not improved:
            break
    final_error = None if best.pcc_q_mvar is None else best.pcc_q_mvar - pcc_target_mvar
    status = (
        "improved"
        if best is not initial
        else "held"
        if final_error is not None and abs(final_error) <= 1e-4
        else "limited"
    )
    return ReactiveTrackingResult(best, initial_error, final_error, count, status)

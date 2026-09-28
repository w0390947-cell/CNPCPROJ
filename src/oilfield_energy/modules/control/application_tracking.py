"""Assess observed PCC response without changing commands or simulating devices.

See docs/modeling/Dynamic_PCC_Tracking.md. Only target changes open a response
window; disturbances and safety overrides do not reset the deadline.
"""

from collections.abc import Sequence
from math import ceil, exp, isfinite, sqrt
from typing import Literal

from .contracts import (
    DynamicTrackingAssessment,
    DynamicTrackingPolicy,
    TrackingStatus,
    TrackingTransition,
)


def _status(values: Sequence[TrackingStatus]) -> TrackingStatus:
    if "violated" in values:
        return "violated"
    return "passed" if values and all(v == "passed" for v in values) else "unknown"


def _allowance(
    error: float | None,
    *,
    startup: bool,
    step: float,
    limit: float,
    ramp: float,
    policy: DynamicTrackingPolicy,
) -> float:
    """Finite reference deadline, not a certificate of physical reachability."""
    if error is None:
        return float(policy.maximum_response_minutes)
    alpha = 1 - exp(-step / max(policy.time_constant_minutes, policy.pv_time_constant_minutes))
    elapsed = step if startup else 0.0
    remaining = error
    for _ in range(ceil(policy.maximum_response_minutes / step) + 1):
        if remaining <= limit:
            return min(float(policy.maximum_response_minutes), max(step, elapsed))
        remaining -= min(alpha * remaining, ramp * step)
        elapsed += step
        if elapsed >= policy.maximum_response_minutes:
            break
    return float(policy.maximum_response_minutes)


def assess_dynamic_tracking(
    *,
    time_minutes: Sequence[float],
    target: Sequence[float],
    actual: Sequence[float | None],
    step_minutes: float,
    unit: Literal["MW", "Mvar"],
    limit: float,
    numerical_tolerance: float,
    policy: DynamicTrackingPolicy,
) -> DynamicTrackingAssessment:
    """Pure evaluation of a complete trajectory or executed prefix.

    A sample labelled t contains the response after [t, t+step]. Deadlines and
    confirmation times therefore use t+step. A missing sample never proves a pass.
    """
    if not isfinite(step_minutes) or step_minutes <= 0:
        raise ValueError("tracking step must be finite and positive")
    if not 0 < numerical_tolerance < limit or not isfinite(limit):
        raise ValueError("tracking tolerances must be finite, positive and separate")
    n = len(time_minutes)
    if not n or len(target) != n or len(actual) != n:
        raise ValueError("dynamic tracking requires aligned nonempty trajectories")
    if any(not isfinite(v) for v in (*time_minutes, *target)) or time_minutes[0] < 0:
        raise ValueError("tracking times and targets must be finite")
    if any(abs(b - a - step_minutes) > 1e-9 for a, b in zip(time_minutes, time_minutes[1:])):
        raise ValueError("dynamic tracking requires a contiguous explicit sample grid")
    change = policy.p_change_threshold_mw if unit == "MW" else policy.q_change_threshold_mvar
    if change >= limit:
        raise ValueError("target change threshold must be smaller than the tracking limit")
    ramp = policy.p_ramp_mw_per_minute if unit == "MW" else policy.q_ramp_mvar_per_minute
    errors = [abs(v - t) if v is not None and isfinite(v) else None for v, t in zip(actual, target)]
    effective_limit = limit + numerical_tolerance
    longest = consecutive = 0.0
    for error in errors:
        consecutive = (
            consecutive + step_minutes if error is not None and error > effective_limit else 0
        )
        longest = max(longest, consecutive)
    # Compare to the segment anchor, so small successive changes cannot drift
    # indefinitely without being recorded. No changes to stored targets.
    starts = [0]
    for i in range(1, n):
        if abs(target[i] - target[starts[-1]]) > change:
            starts.append(i)
    transitions: list[TrackingTransition] = []
    for start, stop in zip(starts, [*starts[1:], n]):
        before = actual[start - 1] if start else actual[0]
        initial_error = (
            abs(target[start] - before) if before is not None and isfinite(before) else None
        )
        allowance = _allowance(
            initial_error,
            startup=start == 0,
            step=step_minutes,
            limit=limit,
            ramp=ramp,
            policy=policy,
        )
        deadline = time_minutes[start] + allowance
        count = 0
        entry: float | None = None
        confirmed: float | None = None
        for i in range(start, stop):
            error = errors[i]
            count = count + 1 if error is not None and error <= effective_limit else 0
            if count >= policy.confirmation_samples:
                entry = time_minutes[i - count + 1] + step_minutes
                confirmed = time_minutes[i] + step_minutes
                break
        after = [
            errors[i]
            for i in range(start, stop)
            if time_minutes[i] + step_minutes >= deadline - 1e-9
        ]
        known_after = [v for v in after if v is not None]
        missing = any(v is None for v in errors[start:stop])
        observed_until = time_minutes[stop - 1] + step_minutes
        response: TrackingStatus = "unknown"
        if entry is not None:
            response = "passed" if entry <= deadline + 1e-9 else "violated"
        elif observed_until >= deadline + (policy.confirmation_samples - 1) * step_minutes:
            response = "unknown" if missing else "violated"
        steady: TrackingStatus = "unknown"
        if any(v > effective_limit for v in known_after):
            steady = "violated"
        elif len(known_after) >= policy.confirmation_samples and not missing:
            steady = "passed"
        if missing and response == "passed":
            response = "unknown"
        reason = (
            "响应期限内进入偏差带，期限后持续满足偏差要求"
            if response == steady == "passed"
            else "响应超时或期限后仍有超限；安全约束另行逐分钟校核"
            if "violated" in (response, steady)
            else "目标再次切换、记录结束或存在缺测，稳定观察证据不足"
        )
        transitions.append(
            TrackingTransition(
                start_minute=time_minutes[start],
                observed_until_minute=observed_until,
                target=target[start],
                target_change=target[start] - target[start - 1] if start else None,
                allowed_response_minutes=allowance,
                deadline_minute=deadline,
                entered_band_after_minutes=None if entry is None else entry - time_minutes[start],
                confirmed_after_minutes=None
                if confirmed is None
                else confirmed - time_minutes[start],
                post_deadline_max_error=max(known_after, default=None),
                post_deadline_samples=len(known_after),
                response_status=response,
                steady_status=steady,
                reason=reason,
            )
        )
    response_status = _status([t.response_status for t in transitions])
    # This timer spans target changes. Repeated reissuing cannot hide nonresponse.
    if longest > policy.maximum_response_minutes:
        response_status = "violated"
    steady_status = _status([t.steady_status for t in transitions])
    known = [(i, v) for i, v in enumerate(errors) if v is not None]
    peak = max(known, key=lambda pair: pair[1]) if known else None
    return DynamicTrackingAssessment(
        unit=unit,
        limit=limit,
        numerical_tolerance=numerical_tolerance,
        status=_status([response_status, steady_status]),
        response_status=response_status,
        steady_status=steady_status,
        raw_max_error=None if peak is None else peak[1],
        raw_max_error_time_minute=None if peak is None else time_minutes[peak[0]],
        rmse=sqrt(sum(v * v for _, v in known) / len(known)) if known else None,
        post_deadline_max_error=max(
            (
                t.post_deadline_max_error
                for t in transitions
                if t.post_deadline_max_error is not None
            ),
            default=None,
        ),
        longest_outside_band_minutes=longest,
        invalid_samples=n - len(known),
        transitions=tuple(transitions),
    )

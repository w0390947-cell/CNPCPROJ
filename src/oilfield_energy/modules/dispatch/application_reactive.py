"""DISPATCH-Q-001: reserve and finite-response planning constraints."""

from math import exp, expm1, isfinite

from .contracts import (
    ReactivePlanningPolicy,
    ReactivePlanningResource,
    ReactivePlanningRow,
)


def plan_reactive_envelope(
    resources: tuple[ReactivePlanningResource, ...],
    *,
    steps: int,
    step_minutes: float,
    time_constant_minutes: float,
    ramp_mvar_per_minute: float,
    policy: ReactivePlanningPolicy,
) -> tuple[ReactivePlanningRow, ...]:
    """Reserve Q at unchanged P; certify a bounded constant-command transition.

    Equal ramp shares conservatively bound total absolute device movement.
    Absent initial observations skip only the first transition.
    """
    if (
        type(steps) is not int
        or steps < 1
        or step_minutes <= 0
        or time_constant_minutes <= 0
        or ramp_mvar_per_minute < 0
        or any(
            not isfinite(v)
            for v in (step_minutes, time_constant_minutes, ramp_mvar_per_minute)
        )
        or len({r.resource_id for r in resources}) != len(resources)
    ):
        raise ValueError("invalid reactive planning grid or dynamics")
    duration = min(step_minutes, policy.transition_minutes)
    alpha = -expm1(-duration / time_constant_minutes)
    decay = exp(-duration / time_constant_minutes)
    utilization = 1 - policy.reserve_fraction
    count = sum(r.controllable for r in resources)
    ramp = ramp_mvar_per_minute * duration / max(count, 1)
    rows: list[ReactivePlanningRow] = []
    for r in resources:
        if (
            not r.resource_id
            or not r.facets
            or any(not isfinite(v) for facet in r.facets for v in facet)
            or (r.initial_q_mvar is not None and not isfinite(r.initial_q_mvar))
        ):
            raise ValueError("invalid reactive planning resource")
        if not r.controllable:
            continue
        for t in range(steps):
            for a, b, upper in r.facets:
                rows.append(
                    ReactivePlanningRow(
                        r.resource_id, t, a, b / utilization, 0.0, upper
                    )
                )
                if t or r.initial_q_mvar is not None:
                    previous = -b * decay / alpha if t else 0.0
                    initial = r.initial_q_mvar if r.initial_q_mvar is not None else 0.0
                    rhs = upper if t else upper + b * decay / alpha * initial
                    rows.append(
                        ReactivePlanningRow(
                            r.resource_id, t, a, b / alpha, previous, rhs
                        )
                    )
            if t or r.initial_q_mvar is not None:
                initial = r.initial_q_mvar if r.initial_q_mvar is not None else 0.0
                for sign in (-1.0, 1.0):
                    rows.append(
                        ReactivePlanningRow(
                            r.resource_id,
                            t,
                            0.0,
                            sign,
                            -sign if t else 0.0,
                            ramp if t else ramp + sign * initial,
                        )
                    )
    return tuple(rows)

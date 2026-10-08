"""DISPATCH-Q-002: unsaturated constant-command reachable inner envelope.

Proof, grid requirements and conservatism: docs/modeling/Reactive_Tracking.md.
"""

from math import exp, expm1, isclose, isfinite

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
    execution_step_minutes: float = 1.0,
) -> tuple[ReactivePlanningRow, ...]:
    """Generate immutable rows without changing inputs or invoking a solver.

    v2 uses equal FIRST-substep ramp shares. At fixed P, a feasible initial Q
    and constant physical command then give decreasing, unsaturated increments.
    The planning interval and transition must contain whole execution substeps.
    Absent initial observations skip only the first transition; partial initial
    coverage is rejected in v2. No first-transition certificate exists without
    an observation. v1 preserves endpoint-only rows for explicit legacy replay.
    Invalid/nonfinite inputs or unsupported grids raise ValueError.
    """
    if (
        type(steps) is not int
        or steps < 1
        or step_minutes <= 0
        or time_constant_minutes <= 0
        or execution_step_minutes <= 0
        or ramp_mvar_per_minute < 0
        or any(
            not isfinite(v)
            for v in (
                step_minutes,
                time_constant_minutes,
                ramp_mvar_per_minute,
                execution_step_minutes,
            )
        )
        or len({r.resource_id for r in resources}) != len(resources)
    ):
        raise ValueError("invalid reactive planning grid or dynamics")
    duration = min(step_minutes, policy.transition_minutes)
    certified = policy.version == "reactive-planning-v2"
    if certified:
        for minutes in (step_minutes, duration):
            substeps = minutes / execution_step_minutes
            # Dimensionless roundoff allowance, never an engineering deadline.
            if (
                not isfinite(substeps)
                or substeps < 1
                or not isclose(substeps, round(substeps), rel_tol=0.0, abs_tol=1e-9)
            ):
                raise ValueError("reactive planning requires whole execution substeps")
        # Interpret roundoff-close durations as exactly n execution substeps.
        duration = round(duration / execution_step_minutes) * execution_step_minutes
        observed = {r.initial_q_mvar is not None for r in resources if r.controllable}
        if len(observed) > 1:
            raise ValueError("initial Q must cover all controllable resources or none")
    alpha = -expm1(-duration / time_constant_minutes)
    decay = exp(-duration / time_constant_minutes)
    if alpha == 0:
        raise ValueError("reactive response is below floating-point resolution")
    utilization = 1 - policy.reserve_fraction
    count = sum(r.controllable for r in resources)
    ramp = ramp_mvar_per_minute * duration / max(count, 1)
    if certified:
        step_alpha = -expm1(-execution_step_minutes / time_constant_minutes)
        if step_alpha == 0:
            raise ValueError(
                "reactive substep response is below floating-point resolution"
            )
        # First movement = alpha_step / alpha_h * (Q_end - Q_start).
        # Later movements decay geometrically, so none activates the shared
        # limiter when every authorized resource satisfies its first-step share.
        ramp = (ramp_mvar_per_minute * execution_step_minutes / max(count, 1)) * (
            alpha / step_alpha
        )
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
                    if certified:
                        # The whole trajectory lies on the segment from the
                        # initial state to the command. Both must satisfy the
                        # SAME capability at this interval's fixed active P.
                        rows.append(
                            ReactivePlanningRow(
                                r.resource_id,
                                t,
                                a,
                                0.0,
                                b if t else 0.0,
                                upper if t else upper - b * initial,
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

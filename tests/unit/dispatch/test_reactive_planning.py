"""Independent capability and transition examples for planning rows."""

from dataclasses import replace
from math import cos, exp, pi, sin

import pytest

from oilfield_energy.modules.dispatch.api import plan_reactive_envelope
from oilfield_energy.modules.dispatch.contracts import (
    ReactivePlanningPolicy,
    ReactivePlanningResource,
    ReactivePlanningRow,
)


def rows(initial=0.8, controllable=True, ramp=10.0):
    return plan_reactive_envelope(
        (
            ReactivePlanningResource(
                "svg", ((0.0, 1.0, 1.0), (0.0, -1.0, 1.0)), initial, controllable
            ),
        ),
        steps=2,
        step_minutes=1.0,
        time_constant_minutes=2.0,
        ramp_mvar_per_minute=ramp,
        policy=ReactivePlanningPolicy(),
    )


def feasible(constraints, q):
    return all(
        row.q_coefficient * q[row.time_index]
        + (
            row.previous_q_coefficient * q[row.time_index - 1]
            if row.previous_q_coefficient
            else 0.0
        )
        <= row.upper + 1e-9
        for row in constraints
    )


def test_reserve_and_minute_reachability_are_separate_limits():
    maximum = 0.8 + (1 - exp(-0.5)) * (1 - 0.8)
    assert feasible(rows(), (maximum, 0.9))
    assert not feasible(
        rows(), (0.9, 0.9)
    )  # Static reserve is met; first minute is unreachable.
    assert not feasible(rows(initial=None), (0.91, 0.91))
    assert feasible(rows(initial=None), (0.9, 0.9))
    assert not feasible(
        rows(initial=None), (-0.9, 0.9)
    )  # Later transitions remain constrained.


def test_ramp_authority_and_invalid_inputs():
    assert not feasible(rows(ramp=0.01), (0.82, 0.82))
    assert rows(controllable=False) == ()
    with pytest.raises(ValueError):
        plan_reactive_envelope(
            (),
            steps=2,
            step_minutes=1,
            time_constant_minutes=0,
            ramp_mvar_per_minute=1,
            policy=ReactivePlanningPolicy(),
        )


def constraint_margin(
    constraints: tuple[ReactivePlanningRow, ...],
    p: dict[str, tuple[float, ...]],
    q: dict[str, tuple[float, ...]],
) -> float:
    return min(
        row.upper
        - row.p_coefficient * p[row.resource_id][row.time_index]
        - row.q_coefficient * q[row.resource_id][row.time_index]
        - (
            row.previous_q_coefficient * q[row.resource_id][row.time_index - 1]
            if row.previous_q_coefficient
            else 0.0
        )
        for row in constraints
    )


def test_five_minute_counterexample_is_excluded_with_unchanged_physical_limits():
    """Audit A1: independent aggregate upper bound excludes EVERY command law."""
    wind_facets = tuple(
        (cos(2 * pi * k / 16), sin(2 * pi * k / 16), 5.5 * cos(pi / 16))
        for k in range(16)
    ) + ((0.0, 1.0, 1.5), (0.0, -1.0, 1.5), (-0.3, 1.0, 0.0), (-0.3, -1.0, 0.0))
    resources = (
        ReactivePlanningResource("w1", wind_facets, 0.0, True),
        ReactivePlanningResource("w2", wind_facets, 0.0, True),
        ReactivePlanningResource("svg", ((0.0, 1.0, 1.8), (0.0, -1.0, 1.8)), 0.0, True),
    )
    p = {"w1": (5.0,), "w2": (5.0,), "svg": (0.0,)}
    unreachable = {"w1": (1.35,), "w2": (1.35,), "svg": (1.5,)}
    legacy = plan_reactive_envelope(
        resources,
        steps=1,
        step_minutes=15,
        time_constant_minutes=2,
        ramp_mvar_per_minute=0.9,
        policy=ReactivePlanningPolicy(version="reactive-planning-v1"),
    )
    current = plan_reactive_envelope(
        resources,
        steps=1,
        step_minutes=15,
        time_constant_minutes=2,
        ramp_mvar_per_minute=0.9,
        policy=ReactivePlanningPolicy(),
    )
    assert len(legacy) == 90
    assert constraint_margin(legacy, p, unreachable) >= -1e-12
    upper_bound = 0.0
    for _ in range(5):
        upper_bound += min(0.9, (1 - exp(-0.5)) * (4.8 - upper_bound))
    assert upper_bound == pytest.approx(4.027453173539971, abs=1e-12)
    assert sum(values[0] for values in unreachable.values()) > upper_bound
    assert constraint_margin(current, p, unreachable) < -0.6
    assert (
        constraint_margin(current, p, {r.resource_id: (0.6,) for r in resources}) >= 0
    )


@pytest.mark.parametrize("sign", [-1.0, 1.0])
def test_first_substep_limit_is_tight_for_constant_command(sign):
    resource = ReactivePlanningResource(
        "svg", ((0.0, 1.0, 10.0), (0.0, -1.0, 10.0)), 0.2, True
    )
    constraints = plan_reactive_envelope(
        (resource,),
        steps=1,
        step_minutes=5,
        time_constant_minutes=2,
        ramp_mvar_per_minute=0.3,
        policy=ReactivePlanningPolicy(reserve_fraction=0),
    )
    # Sum of five geometrically decaying increments when the first is 0.3 Mvar.
    delta = 0.3 * sum(exp(-0.5 * k) for k in range(5))
    assert feasible(constraints, (0.2 + sign * delta,))
    assert not feasible(constraints, (0.2 + sign * (delta + 1e-6),))


def test_initial_state_must_be_physical_at_this_intervals_active_power():
    resource = ReactivePlanningResource(
        "wind", ((-0.3, 1.0, 0.0), (-0.3, -1.0, 0.0)), 0.6, True
    )
    constraints = plan_reactive_envelope(
        (resource,),
        steps=2,
        step_minutes=5,
        time_constant_minutes=2,
        ramp_mvar_per_minute=10,
        policy=ReactivePlanningPolicy(),
    )
    # Both end points and both inferred commands fit; Q_start=.54 does not
    # fit the second interval's fixed P=1 MW, so the full path is uncertified.
    assert (
        constraint_margin(constraints, {"wind": (2.0, 1.0)}, {"wind": (0.54, 0.2)}) < 0
    )
    assert (
        constraint_margin(constraints, {"wind": (2.0, 2.0)}, {"wind": (0.54, 0.2)})
        >= -1e-12
    )
    # A valid physical initial point outside the 10% reserve remains admissible.
    assert feasible(rows(initial=1.0, ramp=10), (0.9, 0.9))
    assert not feasible(rows(initial=1.01, ramp=10), (0.9, 0.9))


def test_zero_ramp_preserves_initial_state_and_missing_initial_is_not_invented():
    assert feasible(rows(initial=0.8, ramp=0), (0.8, 0.8))
    assert not feasible(rows(initial=0.8, ramp=0), (0.79, 0.8))
    assert feasible(rows(initial=None, ramp=0), (0.7, 0.7))
    assert not feasible(rows(initial=None, ramp=0), (0.7, 0.6))


@pytest.mark.parametrize("grid", [0.0, -1.0, float("nan"), float("inf"), 0.7, 2.0])
def test_execution_grid_must_be_positive_finite_and_aligned(grid):
    with pytest.raises(ValueError):
        plan_reactive_envelope(
            (),
            steps=1,
            step_minutes=5,
            time_constant_minutes=2,
            ramp_mvar_per_minute=0.9,
            policy=ReactivePlanningPolicy(),
            execution_step_minutes=grid,
        )


def test_partial_initial_coverage_is_rejected_and_inputs_are_not_modified():
    one = ReactivePlanningResource(
        "one", ((0.0, 1.0, 2.0), (0.0, -1.0, 2.0)), 0.1, True
    )
    missing = replace(one, resource_id="two", initial_q_mvar=None)
    with pytest.raises(ValueError, match="initial Q must cover"):
        plan_reactive_envelope(
            (one, missing),
            steps=1,
            step_minutes=5,
            time_constant_minutes=2,
            ramp_mvar_per_minute=0.9,
            policy=ReactivePlanningPolicy(),
        )
    two = replace(missing, initial_q_mvar=-0.2)
    inputs = (one, two)

    def generate(resources):
        return plan_reactive_envelope(
            resources,
            steps=1,
            step_minutes=5,
            time_constant_minutes=2,
            ramp_mvar_per_minute=0.9,
            policy=ReactivePlanningPolicy(),
        )

    assert set(generate(inputs)) == set(generate(tuple(reversed(inputs))))
    assert inputs == (one, replace(one, resource_id="two", initial_q_mvar=-0.2))


def test_decimal_grid_uses_exactly_the_execution_substeps():
    resource = ReactivePlanningResource(
        "svg", ((0.0, 1.0, 1.0), (0.0, -1.0, 1.0)), 0.0, True
    )

    def generate(duration):
        return plan_reactive_envelope(
            (resource,),
            steps=1,
            step_minutes=duration,
            time_constant_minutes=2,
            ramp_mvar_per_minute=0.9,
            policy=ReactivePlanningPolicy(),
            execution_step_minutes=0.1,
        )

    assert generate(0.3) == generate(0.1 + 0.2) == generate(0.30000000000001)
    with pytest.raises(ValueError, match="whole execution substeps"):
        plan_reactive_envelope(
            (resource,),
            steps=1,
            step_minutes=15,
            time_constant_minutes=2,
            ramp_mvar_per_minute=0.9,
            policy=ReactivePlanningPolicy(transition_minutes=2.5),
        )

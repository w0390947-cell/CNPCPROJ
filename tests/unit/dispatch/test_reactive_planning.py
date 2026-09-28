"""Independent capability and transition examples for planning rows."""

from math import exp

import pytest

from oilfield_energy.modules.dispatch.api import plan_reactive_envelope
from oilfield_energy.modules.dispatch.contracts import (
    ReactivePlanningPolicy,
    ReactivePlanningResource,
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

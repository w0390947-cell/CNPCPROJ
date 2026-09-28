from dataclasses import replace

import pytest

from oilfield_energy.modules.control.api import allocate_active_tracking
from oilfield_energy.modules.control.contracts import ActiveTrackingResource


def resource(name="pv", **changes):
    return replace(
        ActiveTrackingResource(name, 1.0, 1.0, 0.0, 2.0, 0.4, 0.5), **changes
    )


@pytest.mark.parametrize(
    "change,expected,unserved", [(0.3, 1.3, 0), (1, 1.4, 0.6), (-1, 0.6, -0.6)]
)
def test_total_cycle_ramp_and_conservation(change, expected, unserved):
    result = allocate_active_tracking((resource(),), change)
    assert dict(result.responses_mw)["pv"] == pytest.approx(expected)
    assert result.unserved_change_mw == pytest.approx(unserved)
    command = dict(result.commands_mw)["pv"]
    # Independent one-cycle first-order plant, with one ramp allowance.
    actual = 1 + max(-0.4, min(0.4, 0.5 * (command - 1)))
    assert actual == pytest.approx(expected)


def test_plan_response_does_not_consume_a_second_ramp_allowance():
    r = resource(baseline_mw=1.3)
    result = allocate_active_tracking((r,), 0.3)
    assert dict(result.responses_mw)["pv"] == pytest.approx(1.4)
    assert result.unserved_change_mw == pytest.approx(0.2)


def test_frozen_resource_and_priority_overflow_keep_identity():
    resources = (
        resource("locked", controllable=False),
        resource("pv", maximum_mw=1.2),
        resource("wind"),
    )
    result = allocate_active_tracking(resources, 0.4)
    assert dict(result.responses_mw) == pytest.approx(
        {"locked": 1, "pv": 1.1, "wind": 1.3}
    )
    assert resources[1].baseline_mw == 1
    assert result.unserved_change_mw == pytest.approx(0)


def test_cap_drop_is_not_available_for_discretionary_increase():
    r = resource(previous_mw=2, baseline_mw=0.5, maximum_mw=0.5)
    assert allocate_active_tracking((r,), 1).unserved_change_mw == 1


def test_empty_and_zero_ramp_preserve_unserved_request():
    assert allocate_active_tracking((), -2).unserved_change_mw == -2
    assert allocate_active_tracking((resource(ramp_mw=0),), 1).unserved_change_mw == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"alpha": 0},
        {"maximum_mw": -1},
        {"ramp_mw": -1},
        {"baseline_mw": float("nan")},
        {"controllable": 1},
    ],
)
def test_invalid_envelopes_are_rejected(changes):
    with pytest.raises(ValueError):
        resource(**changes)


def test_duplicate_ids_and_nonfinite_request_are_rejected():
    with pytest.raises(ValueError):
        allocate_active_tracking((resource(), resource()), 1)
    with pytest.raises(ValueError):
        allocate_active_tracking((resource(),), float("inf"))

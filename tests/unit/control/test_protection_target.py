"""Independent numerical cases for the signed generation cap semantics."""

import pytest

from oilfield_energy.modules.control.api import project_protection_target


@pytest.mark.parametrize(
    "cap,low,high,target,shortfall",
    [
        (7.5, 0, 1, 1, 0),
        (1, 5.5, 8, 5.5, 4.5),
        (1, 0, 8, 1, 0),
        (0, 0, 0, 0, 0),
        (5.5, 5.5, 5.5, 5.5, 0),
    ],
)
def test_projection_preserves_requirement_and_reports_unattainable_cap(
    cap, low, high, target, shortfall
):
    result = project_protection_target(cap, low, high)
    assert result.required_cap_mw == cap
    assert result.target_mw == target
    assert result.capability_shortfall_mw == shortfall


@pytest.mark.parametrize(
    "inputs",
    [
        (-1, 0, 1),
        (1, -1, 2),
        (1, 2, 1),
        (float("nan"), 0, 1),
        (1, 0, float("inf")),
        (True, 0, 1),
    ],
)
def test_invalid_bounds_cannot_become_commands(inputs):
    with pytest.raises(ValueError):
        project_protection_target(*inputs)

"""Independent numeric checks of policy, absolute rating and MVA intersection."""

import math

import pytest

from oilfield_energy.modules.resources.contracts import WindReactiveCapability, WindReactivePolicy


def test_document_percentages_are_exact_and_distinct():
    assert WindReactivePolicy.GENERAL.ratio == 0.333
    assert WindReactivePolicy.GENERAL.ratio != 1 / 3
    assert WindReactivePolicy.SHANCHENG.ratio == 0.30


@pytest.mark.parametrize(
    "p, ratio, absolute, expected",
    [
        (0.0, 0.333, 1.64, 0.0),
        (3.0, 0.333, 1.64, 0.999),
        (3.0, 0.30, 1.64, 0.9),
        (3.0, 0.333, 0.4, 0.4),
        (3.0, 0.0, 1.64, 0.0),
        (5.0, 0.333, 1.64, math.sqrt(2.5625)),
    ],
)
def test_limits_are_intersected_without_converting_absolute_q_to_a_ratio(
    p, ratio, absolute, expected
):
    capability = WindReactiveCapability(5.25, ratio, absolute)
    assert capability.limit_at(p) == pytest.approx(expected)


@pytest.mark.parametrize(
    "ratio, absolute", [(-0.1, 1.0), (float("nan"), 1.0), (0.3, -1.0), (0.3, float("inf"))]
)
def test_invalid_limits_fail_closed(ratio, absolute):
    with pytest.raises(ValueError):
        WindReactiveCapability(5.25, ratio, absolute)


def test_legacy_converter_only_capacity_is_preserved():
    assert WindReactiveCapability(5.0).limit_at(3.0) == 4.0

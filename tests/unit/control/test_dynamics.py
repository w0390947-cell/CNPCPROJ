"""Independent duration and final ordinary-power invariants."""

from fractions import Fraction
from random import Random

import pytest

from oilfield_energy.modules.control.api import constrain_storage_power, execution_substeps


def test_every_supported_daily_grid_preserves_duration_and_boundaries():
    for count in range(1, 289):
        exact_minutes = Fraction(1440, count)
        if exact_minutes.denominator != 1:
            with pytest.raises(ValueError, match="exactly divide"):
                execution_substeps(float(exact_minutes), 1)
        else:
            repeats = execution_substeps(float(exact_minutes), 1)
            assert repeats * count == 1440
            assert all(i * repeats == i * exact_minutes for i in range(count + 1))
            assert 2.3 * repeats * count / 60 == pytest.approx(2.3 * 24)


@pytest.mark.parametrize(
    "planning,device",
    [(0, 1), (-1, 1), (15, 0), (float("nan"), 1), (15, float("inf")), (True, 1), (0.5, 1), (15, 2)],
)
def test_invalid_or_unrepresentable_grid_is_rejected(planning, device):
    with pytest.raises(ValueError):
        execution_substeps(planning, device)


def test_grid_tolerates_representation_noise_not_resampling():
    assert execution_substeps(15 + 1e-10, 1) == 15
    assert execution_substeps(15, 5) == 3
    with pytest.raises(ValueError):
        execution_substeps(15 + 1e-5, 1)


def test_projection_matches_independent_feasibility_and_nearest_point():
    rng = Random(20260917)
    for _ in range(500):
        previous = rng.uniform(-3, 3)
        requested = rng.uniform(-6, 6)
        lower, upper = -rng.uniform(0, 2.5), rng.uniform(0, 2.5)
        ramp = rng.uniform(0, 1)
        result = constrain_storage_power(
            previous_mw=previous,
            requested_mw=requested,
            minimum_mw=lower,
            maximum_mw=upper,
            maximum_change_mw=ramp,
        )
        assert lower <= result.actual_mw <= upper
        assert result.actual_mw + result.unserved_mw == pytest.approx(requested)
        conflict = previous - ramp > upper or previous + ramp < lower
        assert result.physical_override == conflict
        if conflict:
            assert result.actual_mw == (upper if previous > upper else lower)
        else:
            assert abs(result.actual_mw - previous) <= ramp + 1e-12
            # Every independently sampled feasible alternative is no nearer the request.
            for candidate in (lower, upper, previous - ramp, previous + ramp, requested, 0):
                if lower <= candidate <= upper and abs(candidate - previous) <= ramp:
                    assert abs(result.actual_mw - requested) <= abs(candidate - requested) + 1e-12


@pytest.mark.parametrize("sign", [-1, 1])
def test_two_same_direction_requests_share_one_ramp_allowance(sign):
    result = constrain_storage_power(
        previous_mw=0,
        requested_mw=sign * 1.04,
        minimum_mw=-2.5,
        maximum_mw=2.5,
        maximum_change_mw=0.65,
    )
    assert result.actual_mw == sign * 0.65
    assert result.unserved_mw == pytest.approx(sign * 0.39)
    assert not result.physical_override


def test_zero_ramp_holds_unless_physical_envelope_requires_override():
    assert (
        constrain_storage_power(
            previous_mw=1, requested_mw=-2, minimum_mw=-2.5, maximum_mw=2.5, maximum_change_mw=0
        ).actual_mw
        == 1
    )
    forced = constrain_storage_power(
        previous_mw=1, requested_mw=-2, minimum_mw=-2.5, maximum_mw=0, maximum_change_mw=0
    )
    assert forced.actual_mw == 0
    assert forced.physical_override


@pytest.mark.parametrize(
    "changes",
    [
        dict(previous_mw=float("nan")),
        dict(requested_mw=float("inf")),
        dict(maximum_change_mw=-1),
        dict(minimum_mw=3),
        dict(previous_mw=True),
    ],
)
def test_invalid_projection_inputs_fail_closed(changes):
    inputs = dict(
        previous_mw=0, requested_mw=1, minimum_mw=-2.5, maximum_mw=2.5, maximum_change_mw=0.65
    )
    with pytest.raises(ValueError):
        constrain_storage_power(**(inputs | changes))

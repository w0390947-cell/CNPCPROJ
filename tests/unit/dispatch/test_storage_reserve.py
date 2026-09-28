"""Reserve accounting checked against independent MW/MWh examples."""

from dataclasses import replace

import pytest

from oilfield_energy.modules.dispatch.api import plan_storage_reserve
from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy


def plan(**changes):
    values = dict(
        forecast_scale_mw=(10.0,) * 4,
        step_minutes=15,
        remaining_day_minutes=120,
        initial_energy_mwh=2.0,
        minimum_energy_mwh=0.5,
        maximum_energy_mwh=4.5,
        maximum_power_mw=1.0,
        eta_charge=0.9,
        eta_discharge=0.8,
        policy=StorageReservePolicy(),
    )
    return plan_storage_reserve(**(values | changes))


def test_energy_and_power_reserved_in_both_directions_and_end_of_horizon():
    result = plan()
    assert result.up_mw == (0.25,) * 4
    assert result.minimum_power_mw == (-0.75,) * 4
    assert result.maximum_power_mw == (0.75,) * 4
    assert result.energy_floor_mwh[1:] == pytest.approx((0.578125,) * 4)
    assert result.energy_ceiling_mwh[1:] == pytest.approx((4.44375,) * 4)
    assert result.energy_floor_mwh[0] == result.energy_ceiling_mwh[0] == 2.0
    # End of MPC horizon still has reserve; only the actual study end tapers.
    ended = plan(remaining_day_minutes=60)
    assert ended.energy_floor_mwh[-1] == 0.5
    assert ended.energy_ceiling_mwh[-1] == 4.5


@pytest.mark.parametrize("initial,lower", [(0.5, True), (4.5, False)])
def test_minute_recovery_borrows_reserve_without_falsifying_initial_soc(initial, lower):
    result = plan(
        forecast_scale_mw=(10.0,) * 15, step_minutes=1, initial_energy_mwh=initial
    )
    assert result.energy_floor_mwh[0] == result.energy_ceiling_mwh[0] == initial
    if lower:
        assert result.energy_floor_mwh[1] == pytest.approx(0.5 + 0.078125 / 15)
        assert result.energy_floor_mwh[-1] == pytest.approx(0.578125)
    else:
        assert result.energy_ceiling_mwh[1] == pytest.approx(4.5 - 0.05625 / 15)
        assert result.energy_ceiling_mwh[-1] == pytest.approx(4.44375)


def test_unachievable_reserve_is_explicit_not_clipped_or_certified():
    with pytest.raises(ValueError, match="rating"):
        plan(maximum_power_mw=0.2)
    with pytest.raises(ValueError, match="capacity"):
        plan(maximum_energy_mwh=0.55, initial_energy_mwh=0.5)
    with pytest.raises(ValueError, match="align"):
        replace(plan(), up_mw=(0.25,))

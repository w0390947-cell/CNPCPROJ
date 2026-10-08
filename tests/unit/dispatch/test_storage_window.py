"""Hand-calculated necessary energy bounds; references do not move deadlines."""

import pytest

from oilfield_energy.modules.dispatch.api import storage_window_target


def window(hours, reference=2.4499999755):
    return storage_window_target(
        minimum_mwh=0.5,
        maximum_mwh=5,
        task_minimum_mwh=2.45,
        task_maximum_mwh=2.55,
        remaining_hours=hours,
        charge_limit_mw=2.5,
        discharge_limit_mw=2.5,
        eta_charge=0.95,
        eta_discharge=0.95,
        reference_mwh=reference,
        reference_cost_cny_per_mwh=50,
    )


def test_original_final_interval_is_not_recentred_on_optimized_reference():
    target = window(0)
    assert target.minimum_mwh == 2.45
    assert target.maximum_mwh == 2.55
    assert window(0, reference=4).minimum_mwh == 2.45


def test_remaining_charge_and_discharge_have_different_efficiency_factors():
    target = window(0.25)
    assert target.minimum_mwh == pytest.approx(2.45 - 2.5 * 0.25 * 0.95)
    assert target.maximum_mwh == pytest.approx(2.55 + 2.5 * 0.25 / 0.95)
    # The previously observed 1425 state cannot reach even the ORIGINAL upper
    # bound; this conclusion needs neither a solver nor a sampled oilfield curve.
    assert 3.565021271598951 > target.maximum_mwh


def test_long_horizon_still_respects_physical_energy_envelope():
    target = window(24)
    assert (target.minimum_mwh, target.maximum_mwh) == (0.5, 5)

"""Hand-calculated costs and physical/accounting failure boundaries."""

from dataclasses import FrozenInstanceError, asdict

import pytest

from oilfield_energy.modules.dispatch.api import (
    account_renewable,
    assess_reference_economics,
    evaluate_economics,
)
from oilfield_energy.modules.dispatch.contracts import CostRates, EconomicCost


def test_interval_energy_cost_matches_hand_calculation():
    cost = evaluate_economics(
        price_cny_per_mwh=[100, 200],
        import_mw=[2, 3],
        curtailed_mw=[1, 0.5],
        charge_mw=[1, 0],
        discharge_mw=[0, 2],
        loss_mw=[0.1, 0.2],
        dt_hours=0.5,
        rates=CostRates(10, 20, 30),
    )
    assert asdict(cost) == {
        "import_cost_cny": 400.0,
        "curtailment_cost_cny": 7.5,
        "storage_degradation_cost_cny": 30.0,
        "loss_cost_cny": pytest.approx(4.5),
        "economic_cost_cny": 442.0,
    }


def test_raw_prediction_excess_is_preserved_without_becoming_curtailment():
    raw, available, used = [4.0, 5.0], [2.0, 3.0], [1.0, 2.0]
    result = account_renewable(raw, available, used)
    assert result.nameplate_excess_mw == (2.0, 2.0)
    assert result.curtailed_mw == (1.0, 1.0)
    assert raw == [4.0, 5.0] and available == [2.0, 3.0] and used == [1.0, 2.0]
    raw[0] = 999.0
    assert result.raw_available_mw == (4.0, 5.0)
    with pytest.raises(FrozenInstanceError):
        setattr(result, "curtailed_mw", (0.0, 0.0))


def test_changing_actual_capability_changes_curtailment_even_with_same_dispatch():
    lower = account_renewable([5.0], [2.0], [1.0])
    higher = account_renewable([5.0], [3.0], [1.0])
    assert lower.curtailed_mw == (1.0,)
    assert higher.curtailed_mw == (2.0,)
    assert lower.nameplate_excess_mw == (3.0,)
    assert higher.nameplate_excess_mw == (2.0,)


@pytest.mark.parametrize(
    "raw,available,used",
    [
        ([1], [2], [1]),
        ([1], [1], [1.1]),
        ([1], [1], [-1]),
        ([float("nan")], [1], [1]),
        ([-1e-8], [0], [0]),
        ([1], [1, 2], [1]),
        ([], [], []),
    ],
)
def test_invalid_renewable_balances_are_rejected(raw, available, used):
    with pytest.raises(ValueError):
        account_renewable(raw, available, used)


def test_roundoff_does_not_produce_negative_curtailment_credit():
    assert account_renewable([1.0], [1.0], [1.0 + 1e-8]).curtailed_mw == (0.0,)


@pytest.mark.parametrize(
    "changes",
    [
        {"dt_hours": 0},
        {"dt_hours": float("nan")},
        {"loss_mw": [float("inf")]},
        {"charge_mw": [-0.1]},
        {"price_cny_per_mwh": [100, 100]},
        {"rates": CostRates(1, -1, 1)},
    ],
)
def test_invalid_economic_inputs_are_rejected(changes):
    args = dict(
        price_cny_per_mwh=[100.0],
        import_mw=[1.0],
        curtailed_mw=[0.0],
        charge_mw=[0.0],
        discharge_mw=[0.0],
        loss_mw=[0.0],
        dt_hours=1.0,
        rates=CostRates(1.0, 1.0, 1.0),
    )
    args.update(changes)
    with pytest.raises(ValueError):
        evaluate_economics(**args)


def test_surrogate_penalties_cannot_enter_economic_comparison():
    cost = EconomicCost(100, 20, 10, 5, 135)
    first = assess_reference_economics(
        [cost], centralized_economic_cost_cny=100, surrogate_objective_cny=999, converged=True
    )
    second = assess_reference_economics(
        [cost], centralized_economic_cost_cny=100, surrogate_objective_cny=9999, converged=True
    )
    assert first.gap_percent == second.gap_percent == 35.0
    assert first.realized_regional_economic_cost_cny is None
    assert first.realization_status == "not_computed"


@pytest.mark.parametrize(
    "cost",
    [
        EconomicCost(1, 2, 3, 4, 99),
        EconomicCost(1, -1, 0, 0, 0),
        EconomicCost(float("nan"), 0, 0, 0, 0),
    ],
)
def test_inconsistent_cost_components_cannot_be_ranked(cost):
    with pytest.raises(ValueError):
        assess_reference_economics(
            [cost], centralized_economic_cost_cny=1, surrogate_objective_cny=1, converged=True
        )


@pytest.mark.parametrize(
    "central,converged,status",
    [
        (0, True, "nonpositive_baseline"),
        (-1, True, "nonpositive_baseline"),
        (100, False, "not_converged"),
    ],
)
def test_undefined_or_unconverged_comparisons_have_no_numeric_gap(central, converged, status):
    result = assess_reference_economics(
        [EconomicCost(1, 0, 0, 0, 1)],
        centralized_economic_cost_cny=central,
        surrogate_objective_cny=1,
        converged=converged,
    )
    assert result.gap_percent is None
    assert result.comparison_status == status

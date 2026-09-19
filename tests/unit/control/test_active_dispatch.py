"""Independent capability/energy-free dispatch oracles, in MW."""

from dataclasses import FrozenInstanceError, replace
from random import Random

import pytest

from oilfield_energy.modules.control.api import (
    allocate_wind_storage_target,
    wind_storage_target_bounds,
)
from oilfield_energy.modules.control.contracts import StorageActiveCapability, WindActiveState


def test_weather_drop_redistributes_to_another_turbine_without_changing_measured_input():
    wind = (WindActiveState("a", 4, 1, True), WindActiveState("b", 1, 5, True))
    storage = StorageActiveCapability(0, True, 0, 0)
    plan = allocate_wind_storage_target(wind, storage, 5)
    assert plan is not None
    assert {point.resource_id: point.target_mw for point in plan.wind} == {"a": 1, "b": 4}
    assert plan.storage_target_mw == 0
    assert wind[0].measured_mw == 4
    assert wind_storage_target_bounds(wind, storage) == (0, 6)


def test_charge_priority_then_proportional_wind_reduction():
    wind = (WindActiveState("a", 4, 5, True), WindActiveState("b", 4, 5, True))
    plan = allocate_wind_storage_target(wind, StorageActiveCapability(1, True, 2.5, 0), 4.5)
    assert plan is not None
    assert [point.target_mw for point in plan.wind] == [3.5, 3.5]
    assert plan.storage_target_mw == -2.5


def test_deadband_never_makes_an_unreachable_target_or_wind_power_valid():
    wind = (WindActiveState("a", 3, 1, True),)
    storage = StorageActiveCapability(0, True, 0, 0)
    assert allocate_wind_storage_target(wind, storage, 3, deadband_mw=10) is None
    plan = allocate_wind_storage_target(wind, storage, 0.5, deadband_mw=10)
    assert plan is not None and plan.wind[0].target_mw == 1


def test_fixed_uncontrollable_power_is_preserved_and_empty_interval_rejected():
    wind = (WindActiveState("a", 3, 1, False), WindActiveState("b", 2, 4, True))
    storage = StorageActiveCapability(-2, False, 0, 0)
    assert wind_storage_target_bounds(wind, storage) == (1, 5)
    plan = allocate_wind_storage_target(wind, storage, 4)
    assert plan is not None
    assert [point.target_mw for point in plan.wind] == [None, 3]
    assert plan.storage_target_mw is None
    assert allocate_wind_storage_target(wind, storage, 0) is None
    empty_wind = (WindActiveState("a", 1, 0.5, True),)
    assert wind_storage_target_bounds(empty_wind, storage) == (0, -1.5)
    assert allocate_wind_storage_target(empty_wind, storage, 0) is None


def test_seeded_dispatch_conserves_power_respects_bounds_and_device_permutations():
    rng = Random(60601)
    for _ in range(300):
        wind = tuple(
            WindActiveState(str(i), rng.uniform(0, 5), rng.uniform(0, 5), rng.random() > 0.2)
            for i in range(3)
        )
        controllable = rng.random() > 0.2
        storage = StorageActiveCapability(
            rng.uniform(-2.5, 2.5),
            controllable,
            rng.uniform(0, 2.5) if controllable else 0,
            rng.uniform(0, 2.5) if controllable else 0,
        )
        fixed = sum(w.measured_mw for w in wind if not w.controllable)
        fixed += 0 if controllable else storage.measured_mw
        minimum = max(0, fixed - storage.charge_limit_mw)
        maximum = fixed + sum(w.available_mw for w in wind if w.controllable)
        maximum += storage.discharge_limit_mw
        if minimum > maximum:
            assert allocate_wind_storage_target(wind, storage, 0) is None
            continue
        target = rng.uniform(minimum, maximum)
        plan = allocate_wind_storage_target(wind, storage, target)
        assert plan is not None
        total = 0.0
        for point, state in zip(plan.wind, wind, strict=True):
            assert point.resource_id == state.resource_id
            if state.controllable:
                assert point.target_mw is not None
                assert -1e-9 <= point.target_mw <= state.available_mw + 1e-9
                total += point.target_mw
            else:
                assert point.target_mw is None
                total += state.measured_mw
        if controllable:
            assert plan.storage_target_mw is not None
            assert -storage.charge_limit_mw - 1e-9 <= plan.storage_target_mw
            assert plan.storage_target_mw <= storage.discharge_limit_mw + 1e-9
            total += plan.storage_target_mw
        else:
            assert plan.storage_target_mw is None
            total += storage.measured_mw
        assert total == pytest.approx(target, abs=1e-9, rel=0)
        reversed_plan = allocate_wind_storage_target(tuple(reversed(wind)), storage, target)
        assert reversed_plan is not None
        assert {p.resource_id: p.target_mw for p in reversed_plan.wind} == pytest.approx(
            {p.resource_id: p.target_mw for p in plan.wind}, abs=1e-9, rel=0
        )
        assert allocate_wind_storage_target(wind, storage, maximum + 0.1) is None


def test_boundary_contracts_reject_bad_quality_values_and_duplicate_ids():
    for bad in (float("nan"), float("inf"), -1, True):
        with pytest.raises(ValueError):
            WindActiveState("a", bad, 5, True)
    wind = WindActiveState("a", 3, 1, True)
    with pytest.raises(FrozenInstanceError):
        wind.measured_mw = 1
    with pytest.raises(ValueError, match="unique"):
        wind_storage_target_bounds((wind, wind), StorageActiveCapability(0, True, 0, 0))
    with pytest.raises(ValueError, match="zero"):
        StorageActiveCapability(0, False, 1, 0)
    with pytest.raises(ValueError):
        replace(wind, controllable=1)

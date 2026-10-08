"""Minute reserve feedback uses completed state and declared forecast uncertainty."""

import pytest

from oilfield_energy.modules.control.contracts import DeviceCheckpoint
from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy
from oilfield_energy.workflows.cluster_execution.api import observe_storage_reserve
from oilfield_energy.workflows.cluster_execution.contracts import (
    MinuteReserveObservation,
)


def observe(energy=2.0, power=0.0, **changes):
    return observe_storage_reserve(
        **(
            dict(
                region="A",
                state=DeviceCheckpoint(
                    minute=10, storage_energy_mwh=energy, storage_power_mw=power
                ),
                required_mw=0.25,
                minimum_energy_mwh=0.5,
                maximum_energy_mwh=4.5,
                maximum_power_mw=1.0,
                eta_charge=0.9,
                eta_discharge=0.8,
                remaining_minutes=100,
                policy=StorageReservePolicy(),
            )
            | changes
        )
    )


def test_bidirectional_minute_state_and_endurance():
    assert not observe().deficient
    low = observe(energy=0.51)
    assert low.deficient and low.available_up_mw == pytest.approx(0.032)
    assert low.minute == 10
    high = observe(energy=4.49)
    assert high.deficient and high.available_down_mw == pytest.approx(0.01 / 0.9 / 0.25)
    assert observe(power=0.9).deficient
    assert observe(power=-0.9).deficient
    assert not observe(energy=0.51, remaining_minutes=1).deficient


@pytest.mark.parametrize(
    "energy,power,up,down,deficient",
    [
        (0.52, 0.0, 0.076, 2.0, True),
        (0.52, 0.05, 0.026, 2.05, True),
        (0.52, -0.5, 0.576, 1.5, False),
        (0.55, 0.1, 0.09, 2.1, True),
        (4.48, -0.05, 2.05, 0.0342105263157895, True),
        (4.48, 0.5, 1.5, 0.5842105263157895, False),
        (0.5, -0.5, 0.5, 1.5, False),
        (4.5, 0.5, 1.5, 0.5, False),
        (0.52, 0.5, 0.0, 2.5, True),
        (4.48, -0.5, 2.5, 0.0, True),
    ],
)
def test_incremental_reserve_hand_calculations(energy, power, up, down, deficient):
    """Independent 15-minute MW/MWh examples, including baseline exhaustion."""
    result = observe(
        energy, power, maximum_power_mw=2.0, eta_charge=0.95, eta_discharge=0.95
    )
    assert result.available_up_mw == pytest.approx(up, abs=1e-12)
    assert result.available_down_mw == pytest.approx(down, abs=1e-12)
    assert result.deficient is deficient
    assert result.model_version == "storage-reserve-observation-v2"
    assert result.support_hours == 0.25


@pytest.mark.parametrize("remaining_minutes", [1, 7, 15, 53])
@pytest.mark.parametrize("eta_charge,eta_discharge", [(0.9, 0.8), (1.0, 1.0)])
def test_reserve_endpoints_obey_independent_energy_balance(
    remaining_minutes, eta_charge, eta_discharge
):
    """Integrate targets, rather than repeating the observer's min/max formula."""
    hours = min(remaining_minutes, 15) / 60
    for energy in (0.5, 0.51, 2.0, 4.49, 4.5):
        for power in (-1.0, -0.5, 0.0, 0.5, 1.0):
            result = observe(
                energy,
                power,
                remaining_minutes=remaining_minutes,
                eta_charge=eta_charge,
                eta_discharge=eta_discharge,
            )

            def feasible(target):
                final_energy = energy + hours * (
                    -target / eta_discharge if target >= 0 else -target * eta_charge
                )
                return (
                    abs(target) <= 1.0 + 1e-12
                    and 0.5 - 1e-12 <= final_energy <= 4.5 + 1e-12
                )

            assert result.support_hours == hours
            for direction, margin in (
                (1, result.available_up_mw),
                (-1, result.available_down_mw),
            ):
                endpoint = power + direction * margin
                if margin > 0:
                    assert feasible(endpoint)
                # A zero margin can mean the current baseline is unsustainable.
                assert not feasible(endpoint + direction * 1e-6)


def test_threshold_tolerance_is_recorded_policy_and_does_not_change_units():
    # E=0.5390625 with eta_dis=0.8 supports exactly 0.125 MW for 0.25 h.
    assert not observe(energy=0.5390625).deficient
    assert not observe(energy=0.5390625, power=5e-10).deficient
    assert observe(energy=0.5390625, power=2e-9).deficient
    assert observe(
        energy=0.5390625,
        power=5e-10,
        policy=StorageReservePolicy(observation_tolerance_mw=0),
    ).deficient


@pytest.mark.parametrize("energy,power", [(0.49, 0), (4.51, 0), (2, -1.01), (2, 1.01)])
def test_invalid_physical_state_is_not_reported_as_available_reserve(energy, power):
    with pytest.raises(ValueError, match="physical bounds"):
        observe(energy, power)


@pytest.mark.parametrize(
    "changes",
    [
        {"remaining_minutes": 0},
        {"eta_charge": 0},
        {"eta_discharge": 1.1},
        {"required_mw": float("nan")},
        {"maximum_power_mw": float("inf")},
    ],
)
def test_invalid_observation_inputs_fail_explicitly(changes):
    with pytest.raises(ValueError, match="invalid reserve observation"):
        observe(**changes)


def test_observation_preserves_input_and_historical_semantics():
    state = DeviceCheckpoint(minute=7, storage_energy_mwh=0.52, storage_power_mw=-0.5)
    before = state.model_dump()
    current = observe(state=state)
    assert state.model_dump() == before
    assert (
        MinuteReserveObservation.model_validate_json(current.model_dump_json())
        == current
    )
    historical = current.model_dump(exclude={"model_version", "support_hours"})
    historical.update(available_up_mw=0.064, deficient=True)
    restored = MinuteReserveObservation.model_validate(historical)
    assert restored.model_version is None and restored.support_hours is None
    assert restored.available_up_mw == 0.064 and restored.deficient
    with pytest.raises(ValueError, match="support duration"):
        MinuteReserveObservation.model_validate(
            {**historical, "model_version": current.model_version}
        )


def test_historical_policy_does_not_acquire_reserve_evidence():
    from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy

    assert ExecutionPolicy.model_validate({}).storage_reserve is None


def test_rejected_adjustment_cannot_claim_adoption_or_rewrite_past_minutes():
    from oilfield_energy.workflows.cluster_execution.contracts import (
        ReserveTargetAdjustment,
        RollingUpdate,
    )

    payload = dict(
        minute=7,
        trigger_regions=("A",),
        original_p_mw={"A": (2.0,) * 8},
        original_q_mvar={"A": (0.0,) * 8},
        update=RollingUpdate(
            start_minute=7, end_minute=15, horizon_end_minute=15, status="unknown"
        ),
    )
    assert not ReserveTargetAdjustment(**payload).update.adopted
    with pytest.raises(ValueError, match="rejected"):
        ReserveTargetAdjustment(**(payload | {"adopted_p_mw": {"A": (1.0,) * 8}}))
    with pytest.raises(ValueError, match="prospective"):
        ReserveTargetAdjustment(**(payload | {"minute": 6}))

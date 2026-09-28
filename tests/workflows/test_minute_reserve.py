"""Minute reserve feedback uses completed state and declared forecast uncertainty."""

import pytest

from oilfield_energy.modules.control.contracts import DeviceCheckpoint
from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy
from oilfield_energy.workflows.cluster_execution.api import observe_storage_reserve


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

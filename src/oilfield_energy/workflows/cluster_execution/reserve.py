"""Causal minute-boundary reserve observations; no forecast or plant reader."""

from math import isfinite

from oilfield_energy.modules.control.contracts import DeviceCheckpoint
from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy

from .contracts import MinuteReserveObservation


def observe_storage_reserve(
    *,
    region: str,
    state: DeviceCheckpoint,
    required_mw: float,
    minimum_energy_mwh: float,
    maximum_energy_mwh: float,
    maximum_power_mw: float,
    eta_charge: float,
    eta_discharge: float,
    remaining_minutes: int,
    policy: StorageReservePolicy,
) -> MinuteReserveObservation:
    """Observe constant-target increments relative to the completed net power.

    Positive power discharges. The target can switch instantaneously and stays
    constant for the support interval; ramp, Q and network limits are excluded.
    See docs/modeling/Storage_Reserve.md, observation model v2. Invalid physical
    states raise ValueError instead of being clipped into apparent reserve.
    """
    if (
        remaining_minutes <= 0
        or not 0 < eta_charge <= 1
        or not 0 < eta_discharge <= 1
        or minimum_energy_mwh > maximum_energy_mwh
        or maximum_power_mw <= 0
        or required_mw < 0
        or any(
            not isfinite(v)
            for v in (
                required_mw,
                minimum_energy_mwh,
                maximum_energy_mwh,
                maximum_power_mw,
            )
        )
    ):
        raise ValueError("invalid reserve observation inputs")
    if not (
        minimum_energy_mwh <= state.storage_energy_mwh <= maximum_energy_mwh
        and -maximum_power_mw <= state.storage_power_mw <= maximum_power_mw
    ):
        raise ValueError("reserve observation state exceeds physical bounds")
    hours = min(remaining_minutes, policy.support_minutes) / 60
    maximum_target_mw = min(
        maximum_power_mw,
        (state.storage_energy_mwh - minimum_energy_mwh) * eta_discharge / hours,
    )
    minimum_target_mw = -min(
        maximum_power_mw,
        (maximum_energy_mwh - state.storage_energy_mwh) / eta_charge / hours,
    )
    # Both bounds are absolute targets. Shift to the actual-power baseline only
    # after intersecting energy and power limits, including cross-zero actions.
    up = max(0.0, maximum_target_mw - state.storage_power_mw)
    down = max(0.0, state.storage_power_mw - minimum_target_mw)
    return MinuteReserveObservation(
        minute=state.minute,
        region=region,
        energy_mwh=state.storage_energy_mwh,
        storage_power_mw=state.storage_power_mw,
        required_mw=required_mw,
        available_up_mw=up,
        available_down_mw=down,
        deficient=(
            min(up, down) + policy.observation_tolerance_mw
            < required_mw * policy.trigger_fraction
        ),
        model_version="storage-reserve-observation-v2",
        support_hours=hours,
    )

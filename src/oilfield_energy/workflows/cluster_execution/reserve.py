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
    """Energy endurance and instantaneous P headroom, not a dynamic certificate."""
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
    hours = min(remaining_minutes, policy.support_minutes) / 60
    up = max(
        0.0,
        min(
            maximum_power_mw - state.storage_power_mw,
            (state.storage_energy_mwh - minimum_energy_mwh) * eta_discharge / hours,
        ),
    )
    down = max(
        0.0,
        min(
            maximum_power_mw + state.storage_power_mw,
            (maximum_energy_mwh - state.storage_energy_mwh) / eta_charge / hours,
        ),
    )
    return MinuteReserveObservation(
        minute=state.minute,
        region=region,
        energy_mwh=state.storage_energy_mwh,
        storage_power_mw=state.storage_power_mw,
        required_mw=required_mw,
        available_up_mw=up,
        available_down_mw=down,
        deficient=min(up, down) + 1e-9 < required_mw * policy.trigger_fraction,
    )

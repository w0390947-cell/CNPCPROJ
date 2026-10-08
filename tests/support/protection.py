"""Explicit synthetic telemetry for the retained protection component."""

from datetime import datetime, timezone
from oilfield_energy.shancheng_control import (
    ChannelActuationFeedback,
    SignalValidity,
    ShanchengTelemetry,
    StorageInterlocks,
    StorageTelemetry,
    SvgTelemetry,
    WindTurbineTelemetry,
)


def protection_telemetry(
    *,
    wind_p: float = 8.0,
    wind_available: float = 10.0,
    storage_p: float = 0.0,
    energy_mwh: float = 2.5,
    active_valid: bool = True,
) -> ShanchengTelemetry:
    validity = (
        SignalValidity(True, True) if active_valid else SignalValidity(False, False)
    )
    return ShanchengTelemetry(
        snapshot_id="synthetic-snapshot",
        observed_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        elapsed_minutes=0.0,
        clock_hour=12.0,
        step_minutes=1.0,
        wind_turbines=(
            WindTurbineTelemetry(
                name="WT-AGG",
                active_power_mw=wind_p,
                reactive_power_mvar=0.0,
                available_active_power_mw=wind_available,
                rated_active_power_mw=10.0,
                active_power_validity=validity,
            ),
        ),
        storage=StorageTelemetry(
            active_power_mw=storage_p,
            energy_mwh=energy_mwh,
            energy_min_mwh=0.5,
            energy_max_mwh=5.0,
            charge_power_max_mw=2.5,
            discharge_power_max_mw=2.5,
            interlocks=StorageInterlocks(),
            active_power_validity=validity,
        ),
        svg=SvgTelemetry(reactive_power_mvar=0.0),
        active_actuation=ChannelActuationFeedback(execution_known=True),
        reactive_actuation=ChannelActuationFeedback(execution_known=True),
    )

"""DISPATCH-RESERVE-001: forecast-based bidirectional storage headroom."""

from math import isfinite

from .contracts import StorageReservePlan, StorageReservePolicy


def plan_storage_reserve(
    *,
    forecast_scale_mw: tuple[float, ...],
    step_minutes: float,
    remaining_day_minutes: float,
    initial_energy_mwh: float,
    minimum_energy_mwh: float,
    maximum_energy_mwh: float,
    maximum_power_mw: float,
    eta_charge: float,
    eta_discharge: float,
    policy: StorageReservePolicy,
) -> StorageReservePlan:
    """Reserve for forecast errors; repay initial reserve debt without inventing SOC.

    Coverage tapers only at the study end, never at an intermediate MPC horizon.
    Initial debt is recovered linearly over the declared recovery interval.
    Positive storage power means discharge. No plant observations are inputs.
    """
    values = (
        *forecast_scale_mw,
        step_minutes,
        remaining_day_minutes,
        initial_energy_mwh,
        minimum_energy_mwh,
        maximum_energy_mwh,
        maximum_power_mw,
        eta_charge,
        eta_discharge,
    )
    if (
        not forecast_scale_mw
        or any(not isfinite(v) for v in values)
        or min(forecast_scale_mw) < 0
        or step_minutes <= 0
        or remaining_day_minutes < len(forecast_scale_mw) * step_minutes - 1e-8
        or not minimum_energy_mwh <= initial_energy_mwh <= maximum_energy_mwh
        or maximum_power_mw <= 0
        or not 0 < eta_charge <= 1
        or not 0 < eta_discharge <= 1
    ):
        raise ValueError("invalid forecast or storage reserve inputs")
    power = tuple(
        policy.minimum_error_mw + policy.forecast_error_fraction * v
        for v in forecast_scale_mw
    )
    if max(power) > maximum_power_mw:
        raise ValueError("requested reserve power exceeds storage rating")
    floors, ceilings = [initial_energy_mwh], [initial_energy_mwh]
    for i in range(1, len(power) + 1):
        elapsed = i * step_minutes
        coverage = (
            min(policy.support_minutes, max(0, remaining_day_minutes - elapsed)) / 60
        )
        required = max(power[i - 1 : i + 1])
        floor = minimum_energy_mwh + required * coverage / eta_discharge
        ceiling = maximum_energy_mwh - required * coverage * eta_charge
        recovery = min(1.0, elapsed / policy.recovery_minutes)
        floors.append(
            min(
                floor,
                initial_energy_mwh + recovery * max(0, floor - initial_energy_mwh),
            )
        )
        ceilings.append(
            max(
                ceiling,
                initial_energy_mwh - recovery * max(0, initial_energy_mwh - ceiling),
            )
        )
    return StorageReservePlan(
        up_mw=power,
        down_mw=power,
        energy_floor_mwh=tuple(floors),
        energy_ceiling_mwh=tuple(ceilings),
        minimum_power_mw=tuple(-maximum_power_mw + p for p in power),
        maximum_power_mw=tuple(maximum_power_mw - p for p in power),
    )

"""DISPATCH-RESERVE-001: forecast-based bidirectional storage headroom."""

# pyright: strict
from math import isfinite
from fractions import Fraction
from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.kernel.intervals import Interval

from .contracts import StorageReservePlan, StorageReservePolicy, ReactivePlanningRow


def reserve_energy_bounds(
    *,
    initial_energy_mwh: float,
    initial_power_mw: float,
    minute_targets_mw: tuple[float, ...],
    eta_charge: float,
    eta_discharge: float,
) -> Interval:
    """Energy enclosure for every monotone one-minute transition, including ZOH."""
    if (
        any(
            not isfinite(v)
            for v in (
                initial_energy_mwh,
                initial_power_mw,
                *minute_targets_mw,
                eta_charge,
                eta_discharge,
            )
        )
        or not 0 < eta_charge <= 1
        or not 0 < eta_discharge <= 1
    ):
        raise ValueError("invalid reserve energy enclosure inputs")
    low = high = Fraction(initial_energy_mwh)
    previous = Fraction(initial_power_mw)

    def derivative(power: Fraction) -> Fraction:
        return (
            -power / Fraction(eta_discharge)
            if power >= 0
            else -power * Fraction(eta_charge)
        )

    for value in minute_targets_mw:
        target = Fraction(value)
        low += min(derivative(previous), derivative(target)) / 60
        high += max(derivative(previous), derivative(target)) / 60
        previous = target
    return Interval(low=lower(low), high=upper(high))


def reserve_import_headroom(reserves: tuple[StorageReservePlan, ...]) -> float:
    """Conservative joint down-call budget; final AC certificate still required."""
    if not reserves:
        return 0.0
    if len({len(r.down_mw) for r in reserves}) != 1:
        raise ValueError("reserve import budget requires aligned region time axes")
    return upper(
        max(
            sum((Fraction(r.down_mw[t]) for r in reserves), Fraction(0))
            for t in range(len(reserves[0].down_mw))
        )
    )


def reserve_reactive_hold_rows(
    initial_mvar: dict[str, float], steps: int
) -> tuple[ReactivePlanningRow, ...]:
    """Bounded repair candidate: retain observed Q, still subject to all checks."""
    if (
        steps <= 0
        or not initial_mvar
        or any(not isfinite(q) for q in initial_mvar.values())
    ):
        raise ValueError("reserve reactive repair requires complete finite state")
    return tuple(
        ReactivePlanningRow(rid, t, 0.0, sign, 0.0, sign * q)
        for rid, q in sorted(initial_mvar.items())
        for t in range(steps)
        for sign in (-1.0, 1.0)
    )


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
    recovery_remaining_minutes: float | None = None,
    full_support: bool = False,
    required_floor_mw: float = 0.0,
    delivery_alpha: float | None = None,
    delivery_ramp_mw: float | None = None,
    delivery_headroom_factor: float = 1.0,
) -> StorageReservePlan:
    """Reserve for forecast errors; repay initial reserve debt without inventing SOC.

    Coverage tapers only at the study end, never at an intermediate MPC horizon.
    Initial debt is recovered linearly over the declared recovery interval.
    Positive storage power means discharge. No plant observations are inputs.
    """
    if full_support and recovery_remaining_minutes is None:
        raise ValueError(
            "full reserve service requires an explicit absolute recovery remainder"
        )
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
        max(
            required_floor_mw,
            policy.minimum_error_mw + policy.forecast_error_fraction * v,
        )
        for v in forecast_scale_mw
    )
    remaining = (
        policy.recovery_minutes
        if recovery_remaining_minutes is None
        else recovery_remaining_minutes
    )
    if (
        not isfinite(remaining)
        or remaining < 0
        or not isfinite(required_floor_mw)
        or required_floor_mw < 0
    ):
        raise ValueError("invalid absolute recovery remainder")
    if max(power) > maximum_power_mw:
        raise ValueError("requested reserve power exceeds storage rating")
    floors, ceilings = [initial_energy_mwh], [initial_energy_mwh]
    limits = tuple(maximum_power_mw - r for r in power)
    if delivery_alpha is not None:
        if (
            not 0 < delivery_alpha <= 1
            or delivery_ramp_mw is None
            or delivery_ramp_mw < 0
            or delivery_headroom_factor < 1
        ):
            raise ValueError("invalid delivery tightening parameters")
        limits = tuple(
            max(
                0.0,
                min(
                    limit,
                    delivery_alpha * maximum_power_mw / (2 - delivery_alpha)
                    - delivery_headroom_factor * r,
                    delivery_ramp_mw / 2 - delivery_headroom_factor * r,
                ),
            )
            for r, limit in zip(power, limits, strict=True)
        )
    delivery_limits = limits
    if delivery_alpha is not None:
        # Recovery itself may require charging/discharging outside the eventual
        # all-calls operating band. Physical and ordinary reserve bounds remain.
        limits = tuple(
            maximum_power_mw - power[t] if t * step_minutes < remaining else limit
            for t, limit in enumerate(delivery_limits)
        )
    for i in range(1, len(power) + 1):
        elapsed = i * step_minutes
        coverage = (
            policy.support_minutes
            if full_support
            else min(policy.support_minutes, max(0, remaining_day_minutes - elapsed))
        ) / 60
        required = max(power[i - 1 : i + 1])
        energy_power = (
            required
            if delivery_alpha is None
            else required * delivery_headroom_factor
            + max(delivery_limits[i - 1 : i + 1])
        )
        if delivery_alpha is not None:
            coverage += 1 / 60
        floor = minimum_energy_mwh + energy_power * coverage / eta_discharge
        ceiling = maximum_energy_mwh - energy_power * coverage * eta_charge
        recovery = min(1.0, elapsed / remaining) if remaining else 1.0
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
        minimum_power_mw=tuple(-p for p in limits),
        maximum_power_mw=limits,
    )

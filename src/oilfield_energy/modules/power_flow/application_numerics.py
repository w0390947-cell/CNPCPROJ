"""Pure current and original-equation validation; no solver, topology or I/O.

See docs/modeling/Flow_Convergence.md. Float64/complex128 bus vectors share
one index axis. Inputs are read-only to these operations; outputs are owned.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from numbers import Real

import numpy as np
from numpy.typing import NDArray

from .contracts import PowerBalanceResidual, RadialBalanceBranch, SingularPowerVoltage


def constant_power_current(
    demand_pu: NDArray[np.complex128],
    voltage_pu: NDArray[np.complex128],
    singular_voltage_pu: float,
) -> NDArray[np.complex128]:
    """Do not substitute another voltage into a constant-power equation."""
    if demand_pu.ndim != 1 or voltage_pu.shape != demand_pu.shape:
        raise ValueError("demand and voltage must share one bus axis")
    if not isfinite(singular_voltage_pu) or singular_voltage_pu <= 0:
        raise ValueError("singular voltage threshold must be finite and positive")
    if not np.isfinite(demand_pu).all() or not np.isfinite(voltage_pu).all():
        raise FloatingPointError("nonfinite power-flow iterate")
    nonzero = demand_pu != 0
    if (nonzero & (np.abs(voltage_pu) <= singular_voltage_pu)).any():
        raise SingularPowerVoltage("SINGULAR_CONSTANT_POWER_VOLTAGE")
    current = np.zeros_like(voltage_pu)
    # Exactly zero demand contributes zero current, even at zero voltage.
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        np.divide(demand_pu, voltage_pu, out=current, where=nonzero)
    return np.conj(current)


def assess_power_balance(
    voltage_pu: NDArray[np.complex128],
    demand_pu: NDArray[np.complex128],
    shunt_q_pu: NDArray[np.float64],
    branches: tuple[RadialBalanceBranch, ...],
    slack_index: int,
) -> PowerBalanceResidual:
    """PQ residuals from Ohm's law and nodal KCL, independent of sweep currents.

    Capacitor nominal Q is positive. Slack injection is a free variable; only
    non-slack equations enter the residual norm. No cancellation across buses.
    """
    size = voltage_pu.size
    if voltage_pu.ndim != 1 or demand_pu.shape != (size,) or shunt_q_pu.shape != (size,):
        raise ValueError("voltage, demand and shunt must share one bus axis")
    if not 0 <= slack_index < size:
        raise ValueError("slack index outside bus axis")
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        outgoing = 1j * shunt_q_pu * voltage_pu
        for branch in branches:
            i, j = branch.parent_index, branch.child_index
            if not (0 <= i < size and 0 <= j < size) or i == j:
                raise ValueError("invalid branch bus indices")
            z, tap = branch.impedance_pu, branch.tap_ratio
            if not isfinite(z.real) or not isfinite(z.imag) or abs(z) == 0:
                raise ValueError("invalid branch impedance")
            if not isfinite(tap) or tap <= 0:
                raise ValueError("invalid branch tap")
            current = (voltage_pu[i] / tap - voltage_pu[j]) / z
            outgoing[i] += current / tap
            outgoing[j] -= current
        mismatch = voltage_pu * np.conj(outgoing) + demand_pu
    if not np.isfinite(mismatch).all():
        raise FloatingPointError("nonfinite original-equation residual")
    mismatch[slack_index] = 0
    return PowerBalanceResidual(
        float(np.abs(mismatch.real).max()), float(np.abs(mismatch.imag).max())
    )


def validate_bus_demands(
    bus_ids: tuple[str, ...],
    p_by_bus: Mapping[str, object],
    q_by_bus: Mapping[str, object],
    *,
    quality_valid: object,
    coherent: object,
) -> tuple[str, ...]:
    """Topology-independent snapshot validation with stable legacy reason codes."""
    reasons: list[str] = []
    if quality_valid is not True:
        reasons.append("POINT_QUALITY_INVALID")
    if coherent is not True:
        reasons.append("POINT_INCOHERENT")
    for label, values in (("P", p_by_bus), ("Q", q_by_bus)):
        if set(values) != set(bus_ids):
            reasons.append(f"{label}_BUS_MAPPING_MISMATCH")
        for bus, value in values.items():
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
                reasons.append(f"{label}_INVALID_VALUE:{bus}")
            else:
                try:
                    finite = isfinite(float(value))
                except (OverflowError, ValueError):
                    finite = False
                if not finite:
                    reasons.append(f"{label}_NONFINITE:{bus}")
    return tuple(reasons)

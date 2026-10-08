"""Reference-network data for convex coordination; no network or solver imports."""

from collections.abc import Iterable, Mapping, Sequence
from math import isclose, isfinite
from numbers import Real

from .contracts import (
    CoordinationNetworkCalibration,
    CoordinationRecoveryPolicy,
    FixedShuntReference,
)


def equivalent_coordination_calibration(
    left: CoordinationNetworkCalibration,
    right: CoordinationNetworkCalibration,
    policy: CoordinationRecoveryPolicy,
) -> bool:
    """Compare effective P/Q demand corrections; never infer physical feasibility."""
    if len(left.p_loss_mw) != len(right.p_loss_mw):
        raise ValueError("calibration horizons differ")
    if {s.bus_id: s.nominal_q_mvar for s in left.fixed_shunts} != {
        s.bus_id: s.nominal_q_mvar for s in right.fixed_shunts
    }:
        raise ValueError("calibration operating modes differ")
    return all(
        abs(a - b) <= policy.calibration_tolerance_mw
        for a, b in zip(left.p_loss_mw, right.p_loss_mw, strict=True)
    ) and all(
        abs(
            a
            - sum(s.q_mvar[t] for s in left.fixed_shunts)
            - b
            + sum(s.q_mvar[t] for s in right.fixed_shunts)
        )
        <= policy.calibration_tolerance_mvar
        for t, (a, b) in enumerate(zip(left.q_loss_mvar, right.q_loss_mvar, strict=True))
    )


def build_coordination_calibration(
    *,
    p_loss_mw: Iterable[float],
    q_loss_mvar: Iterable[float],
    shunt_nominal_mvar_by_bus: Mapping[str, float] | None = None,
    shunt_q_mvar_by_bus: Mapping[str, Iterable[float]] | None = None,
) -> CoordinationNetworkCalibration:
    """Copy reference outputs, preserving bus identity, sign and interval order.

    Both shunt mappings must be supplied together and cover identical buses.
    Omitting both declares no shunt evidence; the consumer must check this
    against the current resolved network. Invalid data raises ValueError.
    """
    p_loss = tuple(p_loss_mw)
    q_loss = tuple(q_loss_mvar)
    if shunt_nominal_mvar_by_bus is None or shunt_q_mvar_by_bus is None:
        if shunt_nominal_mvar_by_bus is not None or shunt_q_mvar_by_bus is not None:
            raise ValueError("both nominal and actual fixed shunt reference data are required")
        return CoordinationNetworkCalibration(p_loss, q_loss)
    if shunt_nominal_mvar_by_bus.keys() != shunt_q_mvar_by_bus.keys():
        raise ValueError("fixed shunt reference bus mappings differ")
    shunts: list[FixedShuntReference] = []
    for bus, nominal in sorted(shunt_nominal_mvar_by_bus.items()):
        values = tuple(shunt_q_mvar_by_bus[bus])
        if (
            not bus.strip()
            or not isinstance(nominal, Real)
            or not isfinite(nominal)
            or len(values) != len(p_loss)
            or any(not isinstance(q, Real) or not isfinite(q) for q in values)
        ):
            raise ValueError("invalid fixed shunt reference data")
        if nominal == 0:
            if any(q != 0 for q in values):
                raise ValueError("zero nominal shunt must have zero reference injection")
        else:
            shunts.append(FixedShuntReference(bus, float(nominal), values))
    return CoordinationNetworkCalibration(p_loss, q_loss, tuple(shunts))


def coordination_shunt_injection(
    calibration: CoordinationNetworkCalibration | None,
    *,
    current_nominal_mvar_by_bus: Mapping[str, float],
    steps: int,
) -> tuple[float, ...]:
    """Validate current resolved compensation before summing reference injections.

    Missing evidence is acceptable only when every resolved nominal injection is
    zero. A changed bus, sign, service state or step invalidates the reference.
    This does not refresh voltage or losses after a forecast/topology change.
    """
    if steps <= 0 or any(
        not bus.strip() or not isinstance(q, Real) or not isfinite(q)
        for bus, q in current_nominal_mvar_by_bus.items()
    ):
        raise ValueError("invalid current fixed shunt data")
    current = {b: q for b, q in current_nominal_mvar_by_bus.items() if q != 0}
    if calibration is None:
        if current:
            raise ValueError("active fixed shunts require explicit network calibration")
        return (0.0,) * steps
    if len(calibration.p_loss_mw) != steps:
        raise ValueError("network calibration does not match the coordination horizon")
    reference = {s.bus_id: s.nominal_q_mvar for s in calibration.fixed_shunts}
    if current.keys() != reference.keys() or any(
        not isclose(q, reference[b], rel_tol=1e-12, abs_tol=1e-12) for b, q in current.items()
    ):
        raise ValueError("fixed shunt calibration does not match the current operating mode")
    return tuple(sum(s.q_mvar[t] for s in calibration.fixed_shunts) for t in range(steps))


def slice_coordination_calibration(
    calibration: CoordinationNetworkCalibration, indices: Sequence[int]
) -> CoordinationNetworkCalibration:
    """Select held intervals for a rolling window; repeated indices are allowed."""
    if not indices or any(
        type(i) is not int or not 0 <= i < len(calibration.p_loss_mw) for i in indices
    ):
        raise ValueError("invalid network calibration window indices")
    return CoordinationNetworkCalibration(
        tuple(calibration.p_loss_mw[i] for i in indices),
        tuple(calibration.q_loss_mvar[i] for i in indices),
        tuple(
            FixedShuntReference(s.bus_id, s.nominal_q_mvar, tuple(s.q_mvar[i] for i in indices))
            for s in calibration.fixed_shunts
        ),
    )

"""Banach self-map certificate for every PQ injection in a radial input box.

Floating iterations only propose a centre. All acceptance inequalities are
recomputed with exact rational arithmetic and outward square-root bounds.
See docs/modeling/Conditional_Closed_Loop.md for the map and its assumptions.
"""

from dataclasses import dataclass
from fractions import Fraction as F
from functools import lru_cache
from math import acos, isfinite, tan

from oilfield_energy.kernel.enclosures import lower, magnitude, upper

from .contracts_envelope import ACBoxCertificate, PowerInterval, RadialEnvelope


@dataclass(frozen=True)
class _Complex:
    real: F = F(0)
    imag: F = F(0)

    def __add__(self, other: "_Complex") -> "_Complex":
        return _Complex(self.real + other.real, self.imag + other.imag)

    def __sub__(self, other: "_Complex") -> "_Complex":
        return _Complex(self.real - other.real, self.imag - other.imag)

    def __mul__(self, other: "_Complex") -> "_Complex":
        return _Complex(
            self.real * other.real - self.imag * other.imag,
            self.real * other.imag + self.imag * other.real,
        )

    def scale(self, factor: F) -> "_Complex":
        return _Complex(self.real * factor, self.imag * factor)

    def conjugate(self) -> "_Complex":
        return _Complex(self.real, -self.imag)

    def divide(self, other: "_Complex") -> "_Complex":
        return (self * other.conjugate()).scale(1 / (other.real**2 + other.imag**2))

    def norm(self) -> tuple[F, F]:
        return magnitude(self.real, self.imag)


def _centre(network: RadialEnvelope, demand: list[_Complex]) -> tuple[complex, ...]:
    return _point_voltages(
        network, tuple(complex(float(s.real), float(s.imag)) for s in demand)
    )


def _point_voltages(
    network: RadialEnvelope, demand: tuple[complex, ...]
) -> tuple[complex, ...]:
    n, slack = len(network.buses), network.slack
    voltage = [complex(network.slack_voltage_pu)] * n
    for _ in range(200):
        current = [
            s.conjugate() / v.conjugate() + 1j * b / network.base_mva * v
            for s, v, b in zip(demand, voltage, network.shunt_q_mvar, strict=True)
        ]
        for line in reversed(network.branches):
            current[line.parent] += current[line.child] / line.tap
        updated = voltage.copy()
        updated[slack] = complex(network.slack_voltage_pu)
        for line in network.branches:
            updated[line.child] = (
                updated[line.parent] / line.tap
                - complex(line.r_pu, line.x_pu) * current[line.child]
            )
        if any(
            not isfinite(v.real) or not isfinite(v.imag) or abs(v) < 1e-12
            for v in updated
        ):
            raise ValueError("no finite nonzero voltage centre")
        change = max(abs(a - b) for a, b in zip(updated, voltage, strict=True))
        voltage = updated
        if change < 1e-13:
            break
    return tuple(voltage)


def radial_point_response(
    network: RadialEnvelope, p_mw: tuple[float, ...], q_mvar: tuple[float, ...]
) -> tuple[float, float]:
    """Fixed-demand floating AC response for numerical proposals, not a certificate."""
    if len(p_mw) != len(network.buses) or len(q_mvar) != len(network.buses):
        raise ValueError("point demand bus axis differs")
    demand = tuple(
        complex(p, q) / network.base_mva for p, q in zip(p_mw, q_mvar, strict=True)
    )
    voltage = _point_voltages(network, demand)
    current = [
        s.conjugate() / v.conjugate() + 1j * b / network.base_mva * v
        for s, v, b in zip(demand, voltage, network.shunt_q_mvar, strict=True)
    ]
    for line in reversed(network.branches):
        current[line.parent] += current[line.child] / line.tap
    if any(
        abs(
            voltage[line.child]
            - (
                voltage[line.parent] / line.tap
                - complex(line.r_pu, line.x_pu) * current[line.child]
            )
        )
        > 1e-9
        for line in network.branches
    ):
        raise ValueError("fixed-demand AC iteration did not converge")
    power = (
        voltage[network.slack] * current[network.slack].conjugate() * network.base_mva
    )
    return float(power.real), float(power.imag)


def radial_proposal_margins(
    network: RadialEnvelope,
    p_mw: tuple[PowerInterval, ...],
    q_mvar: tuple[PowerInterval, ...],
) -> tuple[float, ...]:
    """Cheap corner screening for a numerical proposer, never an interval proof.

    Mixed bus corners and inter-sample points still require certify_ac_box.
    This keeps a greedy P correction from exhausting Q headroom during startup.
    """
    margins: list[float] = []
    tangent = tan(acos(network.pf_min))
    for high_p, high_q in ((False, False), (False, True), (True, False), (True, True)):
        p, q = radial_point_response(
            network,
            tuple(x.high if high_p else x.low for x in p_mw),
            tuple(x.high if high_q else x.low for x in q_mvar),
        )
        margins.extend(
            (
                p - network.pcc_min_mw - 1e-5,
                network.pcc_max_mw - p - 1e-5,
                tangent * p - q - 1e-5,
                tangent * p + q - 1e-5,
            )
        )
    return tuple(margins)


@lru_cache(maxsize=4096)
def certify_ac_box(
    network: RadialEnvelope,
    p_mw: tuple[PowerInterval, ...],
    q_mvar: tuple[PowerInterval, ...],
) -> ACBoxCertificate:
    """Certify the whole demand box, including shunts, taps and both branch ends.

    This proves a quasi-static high-voltage AC solution, not electromagnetic
    stability or selection of that solution by a physical transient system.
    Failure of this sufficient test says unproven, never physically infeasible.
    """
    n = len(network.buses)
    if len(p_mw) != n or len(q_mvar) != n:
        raise ValueError("AC box bus axis differs from network")
    base = F(network.base_mva)
    demand = [
        _Complex(
            (F(p.low) + F(p.high)) / (2 * base), (F(q.low) + F(q.high)) / (2 * base)
        )
        for p, q in zip(p_mw, q_mvar, strict=True)
    ]
    uncertainty = [
        magnitude(
            (F(p.high) - F(p.low)) / (2 * base), (F(q.high) - F(q.low)) / (2 * base)
        )[1]
        for p, q in zip(p_mw, q_mvar, strict=True)
    ]
    try:
        proposed = _centre(network, demand)
    except (ValueError, ZeroDivisionError, OverflowError):
        return ACBoxCertificate(certified=False, reason="voltage centre unavailable")
    v = [_Complex(F(z.real), F(z.imag)) for z in proposed]
    norms = [z.norm() for z in v]
    shunts = [F(q) / base for q in network.shunt_q_mvar]
    currents = [
        s.divide(z).conjugate() + _Complex(F(0), b) * z
        for s, z, b in zip(demand, v, shunts, strict=True)
    ]
    for line in reversed(network.branches):
        currents[line.parent] = currents[line.parent] + currents[line.child].scale(
            1 / F(line.tap)
        )
    mapped = v.copy()
    for line in network.branches:
        mapped[line.child] = (
            mapped[line.parent].scale(1 / F(line.tap))
            - _Complex(F(line.r_pu), F(line.x_pu)) * currents[line.child]
        )
    residual = [(a - b).norm()[1] for a, b in zip(mapped, v, strict=True)]
    # At most one solution whose bus magnitudes meet Vmin. This separate
    # pairwise bound makes adjacent accepted discs refer to the same branch;
    # existence and safety still come from the self-map below, not from Vmin.
    unique_current = [
        F(0)
        if i == network.slack
        else (demand[i].norm()[1] + uncertainty[i]) / F(network.voltage_min_pu) ** 2
        + abs(shunts[i])
        for i in range(n)
    ]
    for line in reversed(network.branches):
        unique_current[line.parent] += unique_current[line.child] / F(line.tap)
    unique_gain = [F(0)] * n
    for line in network.branches:
        unique_gain[line.child] = (
            unique_gain[line.parent] / F(line.tap)
            + magnitude(F(line.r_pu), F(line.x_pu))[1] * unique_current[line.child]
        )
    if max(unique_gain) >= 1:
        return ACBoxCertificate(
            certified=False, reason="high-voltage uniqueness bound unavailable"
        )
    # Radius proposals never enter acceptance without exact re-evaluation.
    last_reason = "no self-mapping contraction disc found"
    for exponent in range(-12, 0):
        for factor in (1, 2, 5):
            radius = F(factor, 10 ** (-exponent))
            radii = [F(0) if i == network.slack else radius for i in range(n)]
            floors = [norms[i][0] - radii[i] for i in range(n)]
            if min(floors) <= 0:
                continue
            di = [
                uncertainty[i] / floors[i]
                + demand[i].norm()[1] * radii[i] / (norms[i][0] * floors[i])
                + abs(shunts[i]) * radii[i]
                for i in range(n)
            ]
            lipschitz = [
                F(0)
                if i == network.slack
                else (demand[i].norm()[1] + uncertainty[i]) / floors[i] ** 2
                + abs(shunts[i])
                for i in range(n)
            ]
            for line in reversed(network.branches):
                di[line.parent] += di[line.child] / F(line.tap)
                lipschitz[line.parent] += lipschitz[line.child] / F(line.tap)
            dv, gain = [F(0)] * n, [F(0)] * n
            for line in network.branches:
                zabs = magnitude(F(line.r_pu), F(line.x_pu))[1]
                dv[line.child] = dv[line.parent] / F(line.tap) + zabs * di[line.child]
                gain[line.child] = (
                    gain[line.parent] / F(line.tap) + zabs * lipschitz[line.child]
                )
            if max(gain) >= 1 or any(residual[i] + dv[i] > radii[i] for i in range(n)):
                continue
            voltage_bounds = tuple(
                PowerInterval(low=lower(floors[i]), high=upper(norms[i][1] + radii[i]))
                for i in range(n)
            )
            branch_bounds: list[F] = []
            current_bounds: list[PowerInterval] = []
            for line in network.branches:
                vmax = max(
                    (norms[line.parent][1] + radii[line.parent]) / F(line.tap),
                    norms[line.child][1] + radii[line.child],
                )
                branch_bounds.append(
                    vmax * (currents[line.child].norm()[1] + di[line.child]) * base
                )
                cm = currents[line.child].norm()
                current_bounds.append(
                    PowerInterval(
                        low=lower(max(F(0), cm[0] - di[line.child])),
                        high=upper(cm[1] + di[line.child]),
                    )
                )
            pcc = (
                currents[network.slack]
                .conjugate()
                .scale(F(network.slack_voltage_pu) * base)
            )
            spread = F(network.slack_voltage_pu) * base * di[network.slack]
            plow, phigh = pcc.real - spread, pcc.real + spread
            qlow, qhigh = pcc.imag - spread, pcc.imag + spread
            pf = F(network.pf_min)
            reasons: list[str] = []
            if any(
                F(x.low) < F(network.voltage_min_pu)
                or F(x.high) > F(network.voltage_max_pu)
                for x in voltage_bounds
            ):
                reasons.append("voltage envelope")
            if any(
                s > F(line.rating_mva)
                for s, line in zip(branch_bounds, network.branches, strict=True)
            ):
                reasons.append("branch envelope")
            if plow < F(network.pcc_min_mw) or phigh > F(network.pcc_max_mw):
                reasons.append("PCC import envelope")
            if plow < 0 or max(abs(qlow), abs(qhigh)) ** 2 * pf**2 > plow**2 * (
                1 - pf**2
            ):
                reasons.append("power factor envelope")
            return ACBoxCertificate(
                certified=not reasons,
                reason=", ".join(reasons),
                voltage_center=tuple((z.real, z.imag) for z in proposed),
                voltage_radius_pu=upper(radius),
                contraction_upper=upper(max(gain)),
                high_voltage_uniqueness_upper=upper(max(unique_gain)),
                voltage_bounds=voltage_bounds,
                branch_mva_upper=tuple(upper(x) for x in branch_bounds),
                branch_current_pu_bounds=tuple(current_bounds),
                pcc_p_mw=PowerInterval(low=lower(plow), high=upper(phigh)),
                pcc_q_mvar=PowerInterval(low=lower(qlow), high=upper(qhigh)),
            )
    return ACBoxCertificate(certified=False, reason=last_reason)

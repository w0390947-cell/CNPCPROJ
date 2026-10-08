"""Conditional loss bound from receiving-end thermal and voltage envelopes."""

from fractions import Fraction
from math import inf, isfinite, nextafter

from oilfield_energy.kernel.enclosures import lower, upper
from .contracts_envelope import ACBoxCertificate, PowerInterval, RadialEnvelope


def certified_ac_loss_bounds(
    network: RadialEnvelope,
    certificate: ACBoxCertificate,
) -> tuple[PowerInterval, PowerInterval]:
    """I-squared series losses and fixed shunts, under an existing AC certificate.

    Receiving-end apparent-power bounds and voltage lower bounds bound each
    branch current. These loss bounds do not certify the network themselves.
    """
    failures: set[str] = set(certificate.reason.split(", ")) if certificate.reason else set()
    if (
        not certificate.certified
        and not failures.issubset({"PCC import envelope", "power factor envelope"})
    ) or (
        len(certificate.branch_mva_upper) != len(network.branches)
        or not certificate.voltage_bounds
        or certificate.contraction_upper is None
        or certificate.contraction_upper >= 1
        or certificate.high_voltage_uniqueness_upper is None
        or certificate.high_voltage_uniqueness_upper >= 1
    ):
        raise ValueError("loss bounds require a complete certified AC box")
    plow, lp, qlow, lq = Fraction(0), Fraction(0), Fraction(0), Fraction(0)
    for i, (branch, s) in enumerate(
        zip(network.branches, certificate.branch_mva_upper, strict=True)
    ):
        voltage = certificate.voltage_bounds[branch.child].low
        current_sq = (
            Fraction(s) / (Fraction(network.base_mva) * Fraction(voltage))
        ) ** 2
        current_min_sq = Fraction(0)
        if len(certificate.branch_current_pu_bounds) == len(network.branches):
            current_min_sq = Fraction(certificate.branch_current_pu_bounds[i].low) ** 2
            current_sq = Fraction(certificate.branch_current_pu_bounds[i].high) ** 2
        plow += Fraction(network.base_mva) * Fraction(branch.r_pu) * current_min_sq
        lp += Fraction(network.base_mva) * Fraction(branch.r_pu) * current_sq
        qlow += Fraction(network.base_mva) * Fraction(branch.x_pu) * current_min_sq
        lq += Fraction(network.base_mva) * Fraction(branch.x_pu) * current_sq
    qlo, qhi = qlow, lq
    for q, v in zip(network.shunt_q_mvar, certificate.voltage_bounds, strict=True):
        a, b = -Fraction(q) * Fraction(v.low) ** 2, -Fraction(q) * Fraction(v.high) ** 2
        qlo += min(a, b)
        qhi += max(a, b)
    return PowerInterval(low=lower(plow), high=upper(lp)), PowerInterval(
        low=lower(qlo), high=upper(qhi)
    )


def refine_ac_balance_certificate(
    network: RadialEnvelope,
    certificate: ACBoxCertificate,
    net_p: PowerInterval,
    net_q: PowerInterval,
) -> ACBoxCertificate:
    """Intersect a mathematical AC enclosure with a known aggregate balance.

    The caller must establish the correlation constraint sum(nodal demands)
    in net_p/net_q. Voltage, thermal and contraction bounds remain unchanged.
    """
    try:
        lp, lq = certified_ac_loss_bounds(network, certificate)
    except ValueError:
        return certificate
    if certificate.pcc_p_mw is None or certificate.pcc_q_mvar is None:
        return certificate
    p = PowerInterval(
        low=max(
            certificate.pcc_p_mw.low, lower(Fraction(net_p.low) + Fraction(lp.low))
        ),
        high=min(
            certificate.pcc_p_mw.high, upper(Fraction(net_p.high) + Fraction(lp.high))
        ),
    )
    q = PowerInterval(
        low=max(
            certificate.pcc_q_mvar.low, lower(Fraction(net_q.low) + Fraction(lq.low))
        ),
        high=min(
            certificate.pcc_q_mvar.high, upper(Fraction(net_q.high) + Fraction(lq.high))
        ),
    )
    failures: list[str] = []
    if p.low < network.pcc_min_mw or p.high > network.pcc_max_mw:
        failures.append("PCC import envelope")
    pf = Fraction(network.pf_min)
    if p.low < 0 or max(
        abs(Fraction(q.low)), abs(Fraction(q.high))
    ) ** 2 * pf**2 > Fraction(p.low) ** 2 * (1 - pf**2):
        failures.append("power factor envelope")
    return certificate.model_copy(
        update={
            "certified": not failures,
            "reason": ", ".join(failures),
            "pcc_p_mw": p,
            "pcc_q_mvar": q,
            "balance_scope": "aggregate_constrained_box",
            "aggregate_p_mw": net_p,
            "aggregate_q_mvar": net_q,
        }
    )


def thermal_loss_upper_bound(
    *,
    base_mva: float,
    minimum_voltage_pu: float,
    resistance_and_rating: tuple[tuple[float, float], ...],
) -> float:
    """Return an outward-rounded I²R upper bound in MW.

    Each pair is (r_pu, receiving-end |S| upper bound in MVA), for every
    energized branch, on base_mva. Assumes |V_child| >= minimum_voltage_pu
    THROUGHOUT the interval. Taps need no correction at the receiving end.
    This does not certify those network assumptions. See Cluster_Transition.md.
    """
    if not all(isfinite(x) and x > 0 for x in (base_mva, minimum_voltage_pu)):
        raise ValueError("loss envelope requires positive finite base and voltage")
    if any(
        not isfinite(r) or r < 0 or not isfinite(s) or s <= 0
        for r, s in resistance_and_rating
    ):
        raise ValueError(
            "loss envelope requires nonnegative resistance and positive rating"
        )
    value = sum(
        (Fraction(r) * Fraction(s) ** 2 for r, s in resistance_and_rating), Fraction()
    )
    value /= Fraction(base_mva) * Fraction(minimum_voltage_pu) ** 2
    result = float(value)
    return nextafter(result, inf) if Fraction(result) < value else result

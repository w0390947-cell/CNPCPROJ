"""Independent closed-form and nodal checks of entire-box AC certificates."""

from decimal import Decimal, localcontext
from itertools import product

import numpy as np
from scipy.optimize import root

from oilfield_energy.modules.power_flow.api import (
    certify_ac_box,
    certified_ac_loss_bounds,
    refine_ac_balance_certificate,
)
from oilfield_energy.modules.power_flow.contracts import (
    EnvelopeBranch,
    PowerInterval as I,
    RadialEnvelope,
)


def network():
    return RadialEnvelope(
        network_id="analytic",
        buses=("0", "1"),
        branches=(
            EnvelopeBranch(
                parent=0, child=1, r_pu=0.01, x_pu=0.02, tap=1, rating_mva=10
            ),
        ),
        slack=0,
        base_mva=10,
        slack_voltage_pu=1,
        shunt_q_mvar=(0, 0),
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        pcc_min_mw=0.1,
        pcc_max_mw=5,
        pf_min=0.9,
    )


def test_fast_proposal_point_solver_matches_independent_quadratic_solution():
    from math import sqrt
    from oilfield_energy.modules.power_flow.api import radial_point_response

    p, q, r, x = 0.2, 0.02, 0.01, 0.02
    a = 1 - 2 * (r * p + x * q)
    v2 = (a + sqrt(a * a - 4 * (r * r + x * x) * (p * p + q * q))) / 2
    ell = (p * p + q * q) / v2
    actual = radial_point_response(network(), (0, 2), (0, 0.2))
    np.testing.assert_allclose(
        actual, (10 * (p + r * ell), 10 * (q + x * ell)), atol=1e-9, rtol=0
    )


def test_box_contains_exact_two_bus_high_voltage_solutions_at_corners_and_interior():
    net = network()
    cert = certify_ac_box(
        net,
        (I(low=0, high=0), I(low=1.99, high=2.01)),
        (I(low=0, high=0), I(low=0.19, high=0.21)),
    )
    assert cert.certified
    assert cert.contraction_upper < 1 and cert.high_voltage_uniqueness_upper < 1
    loss_p, loss_q = certified_ac_loss_bounds(net, cert)
    refined = refine_ac_balance_certificate(
        net, cert, I(low=1.99, high=2.01), I(low=0.19, high=0.21)
    )
    assert refined.certified and refined.balance_scope == "aggregate_constrained_box"
    with localcontext() as ctx:
        ctx.prec = 60
        r, x = Decimal.from_float(0.01), Decimal.from_float(0.02)
        for pv, qv in product(np.linspace(1.99, 2.01, 5), np.linspace(0.19, 0.21, 5)):
            p, q = (
                Decimal.from_float(float(pv)) / 10,
                Decimal.from_float(float(qv)) / 10,
            )
            z2, a = r * r + x * x, 1 - 2 * (r * p + x * q)
            ell = (a - (a * a - 4 * z2 * (p * p + q * q)).sqrt()) / (2 * z2)
            ps, qs = 10 * (p + r * ell), 10 * (q + x * ell)
            voltage = (a - z2 * ell).sqrt()
            assert (
                Decimal.from_float(loss_p.low)
                <= 10 * r * ell
                <= Decimal.from_float(loss_p.high)
            )
            assert (
                Decimal.from_float(loss_q.low)
                <= 10 * x * ell
                <= Decimal.from_float(loss_q.high)
            )
            current = ell.sqrt()
            cb = cert.branch_current_pu_bounds[0]
            assert Decimal.from_float(cb.low) <= current <= Decimal.from_float(cb.high)
            assert (
                Decimal.from_float(refined.pcc_p_mw.low)
                <= ps
                <= Decimal.from_float(refined.pcc_p_mw.high)
            )
            assert (
                Decimal.from_float(refined.pcc_q_mvar.low)
                <= qs
                <= Decimal.from_float(refined.pcc_q_mvar.high)
            )
            assert (
                Decimal.from_float(cert.pcc_p_mw.low)
                <= ps
                <= Decimal.from_float(cert.pcc_p_mw.high)
            )
            assert (
                Decimal.from_float(cert.pcc_q_mvar.low)
                <= qs
                <= Decimal.from_float(cert.pcc_q_mvar.high)
            )
            assert (
                Decimal.from_float(cert.voltage_bounds[1].low)
                <= voltage
                <= Decimal.from_float(cert.voltage_bounds[1].high)
            )
            assert float((ps * ps + qs * qs).sqrt()) <= cert.branch_mva_upper[0]


def test_tap_shunt_certificate_contains_independent_nodal_newton_solution():
    net = network().model_copy(
        update={
            "shunt_q_mvar": (0.05, 0.4),
            "branches": (
                EnvelopeBranch(
                    parent=0, child=1, r_pu=0.01, x_pu=0.02, tap=1.01, rating_mva=10
                ),
            ),
        }
    )
    cert = certify_ac_box(
        net,
        (I(low=0.1, high=0.1), I(low=1.99, high=2.01)),
        (I(low=0.01, high=0.01), I(low=0.59, high=0.61)),
    )
    assert cert.certified, cert.reason
    loss_p, loss_q = certified_ac_loss_bounds(net, cert)
    z, tap = 0.01 + 0.02j, 1.01
    for p, q in product((1.99, 2.01), (0.59, 0.61)):
        s = complex(p, q) / 10

        def equation(v):
            voltage = complex(*v)
            residual = (
                (voltage - 1 / tap) / z
                + (s / voltage).conjugate()
                + 1j * 0.04 * voltage
            )
            return [residual.real, residual.imag]

        solved = root(equation, [0.98, 0])
        assert solved.success
        voltage = complex(*solved.x)
        current = (1 / tap - voltage) / z
        pcc = 10 * (current / tap + complex(0.01, -0.001) + 1j * 0.005).conjugate()
        assert loss_p.contains(pcc.real - 0.1 - p)
        assert loss_q.contains(pcc.imag - 0.01 - q)
        assert cert.pcc_p_mw.contains(pcc.real) and cert.pcc_q_mvar.contains(pcc.imag)
        assert cert.voltage_bounds[1].contains(abs(voltage))
        assert (
            max(abs(voltage), 1 / tap) * abs(current) * 10 <= cert.branch_mva_upper[0]
        )


def test_numerically_solvable_points_do_not_bypass_thermal_or_voltage_envelopes():
    p, q = (
        (I(low=0, high=0), I(low=2, high=2)),
        (I(low=0, high=0), I(low=0.2, high=0.2)),
    )
    net = network()
    tight = net.model_copy(
        update={"branches": (net.branches[0].model_copy(update={"rating_mva": 1}),)}
    )
    assert "branch" in certify_ac_box(tight, p, q).reason
    failed = certify_ac_box(tight, p, q)
    assert (
        refine_ac_balance_certificate(
            tight, failed, I(low=2, high=2), I(low=0.2, high=0.2)
        )
        == failed
    )
    import pytest

    with pytest.raises(ValueError, match="complete certified AC"):
        certified_ac_loss_bounds(tight, failed)
    tight = net.model_copy(update={"voltage_min_pu": 0.999})
    assert "voltage" in certify_ac_box(tight, p, q).reason
    tight = net.model_copy(update={"pcc_max_mw": 2.0})
    assert "PCC" in certify_ac_box(tight, p, q).reason

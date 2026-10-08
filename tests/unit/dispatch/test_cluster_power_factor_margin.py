"""Independent geometric check of the tracking-box sufficient margin."""

from itertools import product
from math import acos, hypot, tan

import pytest

from oilfield_energy.modules.dispatch.api import (
    cluster_power_factor_tracking_margin,
    tighten_cluster_power_factor_reference,
)
from oilfield_energy.modules.dispatch.contracts import PCCTrackingLimits


@pytest.mark.parametrize("target", [0.8, 0.9, 0.95, 0.99])
def test_all_tracking_box_vertices_satisfy_pf_on_both_signed_faces(target):
    limits = PCCTrackingLimits(0.05, 0.05, 1e-6)
    margin = cluster_power_factor_tracking_margin(target=target, region_count=3, tracking=limits)
    k = tan(acos(target))
    # Independent PF evaluation at all 2^(2N) vertices, both Q signs.
    for sign in (-1, 1):
        p_ref = 12.0
        q_ref = sign * (k * p_ref - margin)
        for errors in product((-1, 1), repeat=6):
            p = p_ref + sum(errors[:3]) * 0.050001
            q = q_ref + sum(errors[3:]) * 0.050001
            assert p / hypot(p, q) >= target - 2e-15
    assert margin >= 3 * (0.050001 + k * 0.050001)


def test_345_geometry_requires_both_p_and_q_error_terms():
    limits = PCCTrackingLimits(0.04, 0.03, 1e-6)
    margin = cluster_power_factor_tracking_margin(target=0.8, region_count=1, tracking=limits)
    assert margin == pytest.approx(0.06000175)
    # Only accounting for Q errors would be insufficient when P falls.
    assert 3.96 / hypot(3.96, 3.0) < 0.8


@pytest.mark.parametrize("sign", [-1, 1])
def test_feedback_is_monotone_bounded_and_leaves_passing_slots_unchanged(sign):
    limits = PCCTrackingLimits(0.05, 0.05, 1e-6)
    bound = cluster_power_factor_tracking_margin(target=0.8, region_count=3, tracking=limits)
    current = (0.0, 0.0)
    for _ in range(10):
        next_margin = tighten_cluster_power_factor_reference(
            target=0.8,
            region_count=3,
            tracking=limits,
            current_mvar=current,
            realized_p_mw=(12.0, 12.0),
            realized_q_mvar=(0.0, sign * 9.12),
            violated=(False, True),
        )
        assert next_margin[0] == 0 and current[1] <= next_margin[1] <= bound
        current = next_margin
    assert current[1] == bound

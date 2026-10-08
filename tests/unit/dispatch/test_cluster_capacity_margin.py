"""The reference budget includes simultaneous positive tracking errors."""

from fractions import Fraction as F
import pytest
from oilfield_energy.modules.dispatch.api import cluster_import_tracking_margin
from oilfield_energy.modules.dispatch.contracts import PCCTrackingLimits


def test_all_regional_error_extremes_fit_the_unchanged_physical_capacity():
    limits = PCCTrackingLimits(p_mw=0.05, q_mvar=0.02, numerical_tolerance=1e-6)
    margin = cluster_import_tracking_margin(region_count=3, tracking=limits)
    capacity = F(10)
    assert (
        capacity - F(margin) + 3 * (F(limits.p_mw) + F(limits.numerical_tolerance))
        <= capacity
    )
    assert margin == pytest.approx(0.150003)
    with pytest.raises(ValueError):
        cluster_import_tracking_margin(region_count=0, tracking=limits)

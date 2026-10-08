"""Reference import budget for simultaneous bounded regional tracking errors."""

# pyright: strict
from fractions import Fraction as F
from oilfield_energy.kernel.enclosures import upper
from .contracts import PCCTrackingLimits


def cluster_import_tracking_margin(
    *, region_count: int, tracking: PCCTrackingLimits
) -> float:
    """Reserve sum(eps_r) below physical C, including regional numerical allowance.

    Distinct from reserve activation headroom; neither changes physical C.
    """
    if type(region_count) is not int or region_count < 1:
        raise ValueError("capacity margin requires a positive region count")
    return upper(region_count * (F(tracking.p_mw) + F(tracking.numerical_tolerance)))

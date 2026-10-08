"""Project a retained protection cap without relaxing the safety requirement."""

from math import isfinite

from .contracts_protection import ProtectionTarget


def project_protection_target(
    required_cap_mw: float, minimum_mw: float, maximum_mw: float
) -> ProtectionTarget:
    """Use the minimum legal injection if the original cap is unattainable.

    This only constructs a candidate. P/Q coupling, permissions, command
    freshness and device acceptance still belong to the command controller.
    """
    if (
        any(
            isinstance(v, bool) or not isfinite(v) or v < 0
            for v in (required_cap_mw, minimum_mw, maximum_mw)
        )
        or minimum_mw > maximum_mw
    ):
        raise ValueError("protection requires finite nonnegative MW and ordered bounds")
    return ProtectionTarget(
        required_cap_mw=required_cap_mw,
        minimum_mw=minimum_mw,
        maximum_mw=maximum_mw,
        target_mw=min(maximum_mw, max(minimum_mw, required_cap_mw)),
        capability_shortfall_mw=max(0.0, minimum_mw - required_cap_mw),
    )

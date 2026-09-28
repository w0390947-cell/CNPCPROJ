"""Engineering thresholds remain distinct from floating-point residuals."""

import pytest

from oilfield_energy.modules.dispatch.contracts import PCCTrackingLimits
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


def test_boundaries_and_nonfinite_values():
    limits = PCCTrackingLimits(0.05, 0.03, 1e-6)
    assert limits.accepts_p(0.05)
    assert limits.accepts_p(0.05000001)
    assert not limits.accepts_p(0.050002)
    assert limits.accepts_q(0.03000001)
    assert not limits.accepts_q(0.030002)
    for error in (-1, float("inf"), float("nan")):
        assert not limits.accepts_p(error)
        assert not limits.accepts_q(error)


@pytest.mark.parametrize(
    "p,q,residual",
    [
        (0, 0.05, 1e-6),
        (-1, 0.05, 1e-6),
        (0.05, float("nan"), 1e-6),
        (float("inf"), 0.05, 1e-6),
        (0.05, 0.05, 0.05),
        (0.05, 0.05, 0),
    ],
)
def test_invalid_limits_are_rejected(p, q, residual):
    with pytest.raises(ValueError):
        PCCTrackingLimits(p, q, residual)


def test_execution_policy_is_the_single_source_for_bounds_and_checks():
    policy = ExecutionPolicy(p_tracking_tolerance_mw=0.02, q_tracking_tolerance_mvar=0.03)
    assert policy.tracking_limits == PCCTrackingLimits(0.02, 0.03, 1e-6)
    assert "tracking_limits" not in policy.model_dump()
    with pytest.raises(ValueError, match="numerical tolerance"):
        ExecutionPolicy(p_tracking_tolerance_mw=1e-7)

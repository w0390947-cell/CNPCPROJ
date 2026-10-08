"""Adoption and fail-closed semantics independent of a particular AC solver."""

import json
from dataclasses import asdict

import pytest

from oilfield_energy.modules.control.api import select_storage_tracking
from oilfield_energy.modules.control.contracts import StorageTrackingPoint


@pytest.mark.parametrize("error", [-0.4, 0.4])
def test_safe_strict_improvement_is_adopted(error):
    baseline = StorageTrackingPoint(0.2, error, True)
    candidate = StorageTrackingPoint(0.2 + error, error / 10, True)
    result = select_storage_tracking(
        baseline, candidate.power_mw, lambda _: candidate, enabled=True, physical_override=False
    )
    assert result.status == "adopted"
    assert result.selected_mw == candidate.power_mw
    assert result.baseline is baseline
    json.dumps(asdict(result), allow_nan=False)


@pytest.mark.parametrize(
    "error,safe,status",
    [
        (0.01, False, "network_rejected"),
        (None, False, "network_rejected"),
        (0.4, True, "not_improved"),
        (-0.4, True, "not_improved"),
        (0.5, True, "not_improved"),
        (0.4 - 0.5e-9, True, "not_improved"),
    ],
)
def test_rejection_retains_nonzero_baseline(error, safe, status):
    result = select_storage_tracking(
        StorageTrackingPoint(0.2, 0.4, True),
        0.6,
        lambda power: StorageTrackingPoint(power, error, safe),
        enabled=True,
        physical_override=False,
    )
    assert result.status == status
    assert result.selected_mw == 0.2


@pytest.mark.parametrize(
    "safe,error,enabled,override,proposed,status,selected",
    [
        (False, None, True, False, 0.6, "baseline_unsafe", 0.2),
        (False, 0.4, True, False, 0.6, "baseline_unsafe", 0.2),
        (True, 0.4, False, False, 0.6, "blocked", 0.2),
        (True, 0.4, True, False, 0.2, "unchanged", 0.2),
        (False, None, False, True, 0.0, "physical_override", 0.0),
    ],
)
def test_blocked_or_mandatory_physical_action_never_probes(
    safe, error, enabled, override, proposed, status, selected
):
    def forbidden(_):
        pytest.fail("this branch must not evaluate an ordinary candidate")

    result = select_storage_tracking(
        StorageTrackingPoint(0.2, error, safe),
        proposed,
        forbidden,
        enabled=enabled,
        physical_override=override,
    )
    assert (result.status, result.selected_mw, result.candidate) == (status, selected, None)


@pytest.mark.parametrize(
    "power,error,safe",
    [
        (float("nan"), 0.0, True),
        (0.0, float("inf"), False),
        (0.0, None, True),
        (True, 0.0, True),
        (0.0, 0.0, 1),
    ],
)
def test_invalid_assessment_cannot_be_a_safe_certificate(power, error, safe):
    with pytest.raises(ValueError):
        StorageTrackingPoint(power, error, safe)


def test_mismatched_candidate_assessment_is_rejected():
    with pytest.raises(ValueError, match="proposed storage power"):
        select_storage_tracking(
            StorageTrackingPoint(0.2, 0.4, True),
            0.6,
            lambda _: StorageTrackingPoint(0.5, 0.0, True),
            enabled=True,
            physical_override=False,
        )

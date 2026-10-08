"""Storage candidate adoption; network evaluation is a read-only injected port."""

# pyright: strict

from collections.abc import Callable
from math import isfinite

from .contracts import (
    STORAGE_TRACKING_IMPROVEMENT_MW,
    StorageTrackingPoint,
    StorageTrackingResult,
    StorageTrackingStatus,
)


def select_storage_tracking(
    baseline: StorageTrackingPoint,
    proposed_mw: float,
    evaluate: Callable[[float], StorageTrackingPoint],
    *,
    enabled: bool,
    physical_override: bool,
) -> StorageTrackingResult:
    """Adopt only a safe improvement; probes cannot commit energy or commands.

    Physical/ramp conflicts are mandatory physical corrections, not ordinary
    tracking candidates. An unsafe/unknown baseline is retained as unsafe and
    left to the caller's existing safety path; it is never certified by rollback.
    """
    if isinstance(proposed_mw, bool) or not isfinite(proposed_mw):
        raise ValueError("storage proposal must be finite")
    if type(enabled) is not bool or type(physical_override) is not bool:
        raise ValueError("storage tracking permissions must be explicit")

    def result(
        status: StorageTrackingStatus,
        candidate: StorageTrackingPoint | None = None,
        *,
        selected: float = baseline.power_mw,
    ) -> StorageTrackingResult:
        return StorageTrackingResult(baseline, candidate, proposed_mw, selected, status)

    if physical_override:
        return result("physical_override", selected=proposed_mw)
    if not baseline.safe:
        return result("baseline_unsafe")
    if not enabled:
        return result("blocked")
    if proposed_mw == baseline.power_mw:
        return result("unchanged")
    candidate = evaluate(proposed_mw)
    if candidate.power_mw != proposed_mw:
        raise ValueError("AC evidence must describe the proposed storage power")
    if not candidate.safe:
        return result("network_rejected", candidate)
    # Both safe assessments have finite errors, enforced by the public contract.
    assert baseline.error_mw is not None and candidate.error_mw is not None
    if abs(baseline.error_mw) - abs(candidate.error_mw) <= STORAGE_TRACKING_IMPROVEMENT_MW:
        return result("not_improved", candidate)
    return result("adopted", candidate, selected=proposed_mw)

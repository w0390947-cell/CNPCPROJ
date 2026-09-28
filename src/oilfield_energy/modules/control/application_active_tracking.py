"""Residual active tracking allocation; see docs/modeling/Active_Tracking.md."""

from math import isfinite

from .contracts import ActiveTrackingAllocation, ActiveTrackingResource


def allocate_active_tracking(
    resources: tuple[ActiveTrackingResource, ...],
    requested_change_mw: float,
) -> ActiveTrackingAllocation:
    """Allocate injection change in explicit priority order, preserving input IDs.

    Bounds include the first-order response and total ramp from cycle start.
    A provisional response outside those bounds (e.g. mandatory availability
    clipping) can be retained, but never used to expand discretionary movement.
    Frozen devices retain their baseline. Caller independently checks network
    safety and executes the proposal; no future measurements enter this rule.
    """
    if type(resources) is not tuple or len({r.resource_id for r in resources}) != len(
        resources
    ):
        raise ValueError(
            "active tracking requires an immutable unique-ID resource tuple"
        )
    if isinstance(requested_change_mw, bool) or not isfinite(requested_change_mw):
        raise ValueError("tracking change must be finite MW")
    remaining = requested_change_mw
    responses: list[tuple[str, float]] = []
    commands: list[tuple[str, float | None]] = []
    for r in resources:
        response = r.baseline_mw
        command = None
        if r.controllable:
            low = max(
                r.minimum_mw,
                r.previous_mw - r.ramp_mw,
                r.previous_mw + r.alpha * (r.minimum_mw - r.previous_mw),
            )
            high = min(
                r.maximum_mw,
                r.previous_mw + r.ramp_mw,
                r.previous_mw + r.alpha * (r.maximum_mw - r.previous_mw),
            )
            if low <= high:
                if remaining > 0:
                    response += min(remaining, max(0.0, high - response))
                elif remaining < 0:
                    response -= min(-remaining, max(0.0, response - low))
            # Invert the response only for a changed output, preserving a
            # mandatory physical clipping response when no movement is possible.
            if response != r.baseline_mw:
                command = min(
                    r.maximum_mw,
                    max(
                        r.minimum_mw,
                        r.previous_mw + (response - r.previous_mw) / r.alpha,
                    ),
                )
            remaining -= response - r.baseline_mw
        responses.append((r.resource_id, response))
        commands.append((r.resource_id, command))
    return ActiveTrackingAllocation(
        requested_change_mw, tuple(responses), tuple(commands), remaining
    )

"""Deterministic replay checks that saved evidence belongs to this control chain."""

# pyright: strict
from .contracts_safety import ClusterSafetyEvidence
from .safety_controller import ClusterSafetyController


def replay_cluster_safety(evidence: ClusterSafetyEvidence) -> int:
    """Recompute all executed transitions and continuations from explicit inputs.

    This is a replay check, not an independent alternative AC solver or proof.
    Failed initial certificates are not accepted as successful executions.
    """
    if evidence.initial_continuation is None:
        raise ValueError("no initial continuation to replay")
    controller = ClusterSafetyController(evidence.domain)
    if controller.plan != evidence.initial_continuation:
        raise ValueError("initial continuation replay differs")
    events = {e.start_minute: e for e in evidence.reserve_events}
    if len(events) != len(evidence.reserve_events):
        raise ValueError("duplicate reserve event start")
    for recorded in evidence.steps:
        if recorded.minute in events:
            if controller.accept_reserve_event() != events[recorded.minute]:
                raise ValueError("reserve acceptance replay differs")
        actual = controller.advance(
            recorded.observed,
            recorded.commands if recorded.source == "economic" else None,
            recorded.reason,
            reserve_requests=tuple(a.requested_pcc_up_mw for a in recorded.activation)
            if recorded.activation
            else None,
        )
        if actual != recorded:
            raise ValueError(
                f"executed safety replay differs at minute {recorded.minute}"
            )
    if controller.minute in events:
        if controller.accept_reserve_event() != events[controller.minute]:
            raise ValueError("pending reserve acceptance replay differs")
    if (
        tuple(controller.plans) != evidence.continuations
        or tuple(controller.events) != evidence.reserve_events
    ):
        raise ValueError("continuation/event journal replay differs")
    return len(controller.steps)

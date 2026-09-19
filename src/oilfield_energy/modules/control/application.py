"""Deterministic freshness/ordering policy for coordination messages."""

from .contracts import MessageCursor


def accept_coordination_message(
    previous: MessageCursor,
    incoming: MessageCursor,
    *,
    now_tick: int,
    maximum_age_ticks: int,
) -> bool:
    """Reject replay, older epoch/send time, future and expired messages.

    New sends within the current epoch are legitimate heartbeats. A duplicated
    or out-of-order send cannot refresh freshness. Autonomous status is handled
    separately and never overwrites a coordinated plan with epoch -1.
    """
    if maximum_age_ticks < 0 or now_tick < 0:
        raise ValueError("communication clock and age bound must be nonnegative")
    return (
        incoming.epoch >= 0
        and incoming.epoch >= previous.epoch
        and incoming.sent_tick > previous.sent_tick
        and 0 <= now_tick - incoming.sent_tick <= maximum_age_ticks
    )

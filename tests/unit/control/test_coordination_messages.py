import pytest

from oilfield_energy.modules.control.api import accept_coordination_message
from oilfield_energy.modules.control.contracts import MessageCursor


@pytest.mark.parametrize(
    "incoming,accepted",
    [
        (MessageCursor(1, 9), False),  # New delivery of an older epoch.
        (MessageCursor(2, 8), False),  # Duplicate send.
        (MessageCursor(2, 7), False),  # Same epoch, reordered send.
        (MessageCursor(2, 9), True),  # Same epoch, new heartbeat.
        (MessageCursor(3, 9), True),
        (MessageCursor(3, 11), False),  # Future send time.
        (MessageCursor(-1, 9), False),  # Autonomous status is not a coordinated plan.
    ],
)
def test_epoch_and_send_clock_are_monotone(incoming, accepted):
    assert (
        accept_coordination_message(
            MessageCursor(2, 8),
            incoming,
            now_tick=10,
            maximum_age_ticks=3,
        )
        is accepted
    )


def test_expired_message_cannot_refresh_freshness():
    assert not accept_coordination_message(
        MessageCursor(0, 1),
        MessageCursor(1, 2),
        now_tick=10,
        maximum_age_ticks=3,
    )

"""Pure event applicability, communication schedule and scope rules."""

from collections.abc import Sequence

from .contracts import (
    CommunicationEventEffect,
    CommunicationFaultWindow,
    EventTimeAxis,
    EventType,
    ScenarioEvent,
)


def validate_scenario_events(
    scenario: str,
    region: str,
    events: Sequence[ScenarioEvent],
    maximum_tick: int,
    available_regions: Sequence[str] | None = None,
) -> None:
    """Reject unsupported study conditions before any optimization starts."""
    if len(events) > 20 or len({event.event_id for event in events}) != len(events):
        raise ValueError("at most 20 events with unique event_id values are supported")
    for event in events:
        if scenario == "group_control":
            raise ValueError("group_control runs a fixed suite; custom events are unsupported")
        communication = event.time_axis is EventTimeAxis.COORDINATION_ITERATION
        if communication and scenario != "communication_fault":
            raise ValueError(
                f"event {event.event_id}: communication events require communication_fault"
            )
        if communication and event.end > maximum_tick:
            raise ValueError(f"event {event.event_id}: end exceeds ADMM iteration budget")
        if scenario == "single_microgrid" and event.target != region:
            raise ValueError(f"event {event.event_id}: target is outside the selected microgrid")
        if available_regions is not None and event.target not in available_regions:
            raise ValueError(f"event {event.event_id}: target is absent from the supplied case")


def compile_communication_events(
    events: Sequence[ScenarioEvent],
) -> tuple[CommunicationFaultWindow, ...]:
    """Canonical ordering; no event is replaced by another list entry."""
    windows: list[CommunicationFaultWindow] = []
    for event in events:
        if event.event_type is EventType.COMMUNICATION_OUTAGE:
            windows.append(
                CommunicationFaultWindow(
                    event_id=event.event_id,
                    kind="outage",
                    scope="region",
                    target=event.target,
                    start=int(event.start),
                    end=int(event.end),
                    probability=1.0,
                )
            )
        elif event.event_type is EventType.COMMUNICATION_PACKET_LOSS:
            windows.append(
                CommunicationFaultWindow(
                    event_id=event.event_id,
                    kind="packet_loss",
                    scope="global",
                    target=None,
                    start=int(event.start),
                    end=int(event.end),
                    probability=event.magnitude,
                )
            )
    return tuple(sorted(windows, key=lambda window: window.event_id))


def active_communication_events(
    events: Sequence[CommunicationFaultWindow], sender: str, receiver: str, tick: int
) -> tuple[CommunicationFaultWindow, ...]:
    """Outages match either endpoint; global packet loss matches every link."""
    return tuple(
        event
        for event in events
        if event.start <= tick <= event.end
        and (event.scope == "global" or event.target in {sender, receiver})
    )


def communication_event_effect(
    events: Sequence[CommunicationFaultWindow], sender: str, receiver: str, tick: int
) -> CommunicationEventEffect:
    """Union outages; use maximum active global loss probability, independent of order."""
    active = active_communication_events(events, sender, receiver, tick)
    losses = [event.probability for event in active if event.kind == "packet_loss"]
    return CommunicationEventEffect(
        active=active,
        outage=any(event.kind == "outage" for event in active)
        if any(event.kind == "outage" for event in events)
        else None,
        loss_probability=max(losses, default=0.0)
        if any(event.kind == "packet_loss" for event in events)
        else None,
        loss_window_active=bool(losses),
    )

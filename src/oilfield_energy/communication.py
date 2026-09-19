"""同一Python进程内的可重复通信链路仿真。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, List

import numpy as np

from .hierarchy_types import CommunicationConfig, CommunicationMetrics
from .modules.studies.api import communication_event_effect
from .modules.studies.contracts import CommunicationEventExecution


@dataclass
class _EventObservation:
    ticks: set[int] = field(default_factory=set)
    messages: int = 0
    dropped: int = 0


@dataclass
class _QueuedMessage:
    sender: str
    receiver: str
    payload: Any
    sent_iteration: int
    deliver_iteration: int


class SimulatedCommunicationChannel:
    def __init__(self, config: CommunicationConfig):
        if not 0.0 <= config.loss_probability <= 1.0:
            raise ValueError("loss_probability must be in [0, 1]")
        if config.min_delay_iterations < 0 or config.max_delay_iterations < config.min_delay_iterations:
            raise ValueError("invalid delay range")
        self.config = config
        self.metrics = CommunicationMetrics()
        self._rng = np.random.default_rng(config.random_seed)
        self._queue: List[_QueuedMessage] = []
        if len({event.event_id for event in config.events}) != len(config.events):
            raise ValueError("duplicate communication event ID")
        self._event_observations = {event.event_id: _EventObservation() for event in config.events}

    def _is_outage(self, sender: str, receiver: str, iteration: int) -> bool:
        c = self.config
        if c.outage_region is None or c.outage_start_iteration is None or c.outage_end_iteration is None:
            return False
        involves_region = c.outage_region in {sender, receiver}
        return involves_region and c.outage_start_iteration <= iteration <= c.outage_end_iteration

    def _is_loss_window(self, iteration: int) -> bool:
        c = self.config
        if c.loss_start_iteration is None and c.loss_end_iteration is None:
            return True
        if c.loss_start_iteration is None or c.loss_end_iteration is None:
            raise ValueError("loss window requires both start and end iterations")
        if c.loss_end_iteration < c.loss_start_iteration:
            raise ValueError("loss window end cannot precede start")
        return c.loss_start_iteration <= iteration <= c.loss_end_iteration

    def send(self, sender: str, receiver: str, payload: Any, iteration: int) -> bool:
        self.metrics.sent += 1
        effect = communication_event_effect(self.config.events, sender, receiver, iteration)
        outage = (
            effect.outage
            if effect.outage is not None
            else self._is_outage(sender, receiver, iteration)
        )
        probability = (
            effect.loss_probability
            if effect.loss_probability is not None
            else self.config.loss_probability
        )
        # One random decision per message; overlapping probabilities use maximum severity.
        loss_active = (
            effect.loss_window_active
            if effect.loss_probability is not None
            else self._is_loss_window(iteration)
        )
        dropped = outage or (loss_active and float(self._rng.random()) < probability)
        for event in effect.active:
            observed = self._event_observations[event.event_id]
            observed.ticks.add(iteration)
            observed.messages += 1
            observed.dropped += int(dropped)
        if outage:
            self.metrics.dropped += 1
            self.metrics.outage_dropped += 1
            return False
        if dropped:
            self.metrics.dropped += 1
            return False
        delay = int(self._rng.integers(
            self.config.min_delay_iterations,
            self.config.max_delay_iterations + 1,
        ))
        if delay > 0:
            self.metrics.delayed += 1
        self._queue.append(_QueuedMessage(
            sender=sender,
            receiver=receiver,
            payload=copy.deepcopy(payload),
            sent_iteration=iteration,
            deliver_iteration=iteration + delay,
        ))
        return True

    def event_executions(self) -> tuple[CommunicationEventExecution, ...]:
        records: list[CommunicationEventExecution] = []
        for event in self.config.events:
            observed = self._event_observations[event.event_id]
            records.append(
                CommunicationEventExecution(
                    window=event,
                    status=(
                        "executed"
                        if len(observed.ticks) == event.end - event.start + 1
                        else "partially_executed"
                        if observed.ticks
                        else "not_reached"
                    ),
                    observed_ticks=tuple(sorted(observed.ticks)),
                    matched_messages=observed.messages,
                    dropped_while_active=observed.dropped,
                )
            )
        return tuple(records)

    def receive(self, receiver: str, iteration: int) -> List[_QueuedMessage]:
        ready = [
            item for item in self._queue
            if item.receiver == receiver and item.deliver_iteration <= iteration
        ]
        if ready:
            ready_ids = {id(item) for item in ready}
            self._queue = [item for item in self._queue if id(item) not in ready_ids]
            self.metrics.delivered += len(ready)
        return sorted(ready, key=lambda item: (item.sent_iteration, item.deliver_iteration))

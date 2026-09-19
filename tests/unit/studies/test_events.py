"""Study event contracts and deterministic, order-independent fault semantics."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from oilfield_energy.modules.studies.api import (
    communication_event_effect,
    compile_communication_events,
    validate_scenario_events,
)
from oilfield_energy.modules.studies.contracts import EventType, ScenarioEvent
from oilfield_energy.scenario_events import ScenarioEvent as LegacyEvent


def event(kind=EventType.COMMUNICATION_OUTAGE, **changes):
    payload = dict(
        event_id="e1",
        event_type=kind,
        target="SC",
        start=3,
        end=8,
        time_axis="coordination_iteration"
        if kind.value.startswith("communication")
        else "clock_minute",
        magnitude=1.0,
    )
    return ScenarioEvent.model_validate(payload | changes)


@pytest.mark.parametrize("kind", list(EventType))
@pytest.mark.parametrize(
    "scenario", ["single_microgrid", "cluster_coordination", "communication_fault", "group_control"]
)
def test_request_applicability_matrix(kind, scenario):
    supported = scenario != "group_control" and (
        not kind.value.startswith("communication") or scenario == "communication_fault"
    )
    if supported:
        validate_scenario_events(scenario, "SC", [event(kind)], 220)
    else:
        with pytest.raises(ValueError):
            validate_scenario_events(scenario, "SC", [event(kind)], 220)


@pytest.mark.parametrize(
    "start,end", [(0, 3), (1.5, 3), (1, 3.5), (1, float("inf")), (float("nan"), 3)]
)
def test_communication_ticks_cannot_be_truncated_or_nonfinite(start, end):
    with pytest.raises(ValidationError):
        event(start=start, end=end)


def test_scope_identity_budget_and_legacy_alias():
    assert LegacyEvent is ScenarioEvent
    for events, scenario, region, budget, available in [
        ([event(), event()], "communication_fault", "SC", 220, None),
        ([event(EventType.LOAD_DROP, target="YA_B")], "single_microgrid", "SC", 220, None),
        ([event()], "communication_fault", "SC", 5, None),
        ([event()], "communication_fault", "SC", 220, ["YA_B"]),
    ]:
        with pytest.raises(ValueError):
            validate_scenario_events(scenario, region, events, budget, available)
    with pytest.raises(ValidationError):
        event().start = 10


def test_fault_union_global_probability_and_permutation():
    entries = [
        event(),
        event(event_id="e2", target="YA_B", start=6, end=10),
        event(EventType.COMMUNICATION_PACKET_LOSS, event_id="e3", magnitude=0.3),
        event(EventType.COMMUNICATION_PACKET_LOSS, event_id="e4", magnitude=0.7, start=5, end=12),
    ]
    windows = compile_communication_events(entries)
    assert windows == compile_communication_events(list(reversed(entries)))
    assert entries[0].start == 3
    effect = communication_event_effect(windows, "coordinator", "SC", 6)
    assert effect.outage and effect.loss_probability == 0.7
    unaffected = communication_event_effect(windows, "YA_C", "coordinator", 6)
    assert not unaffected.outage and unaffected.loss_probability == 0.7
    assert len(unaffected.active) == 2
    assert all(w.scope == "global" and w.target is None for w in unaffected.active)
    boundary = communication_event_effect(windows, "SC", "coordinator", 8)
    assert boundary.outage
    assert not communication_event_effect(windows, "SC", "coordinator", 9).outage
    inactive = communication_event_effect(windows, "SC", "coordinator", 20)
    assert (
        inactive.outage is False
        and inactive.loss_probability == 0
        and not inactive.loss_window_active
    )
    defaults = communication_event_effect((), "SC", "coordinator", 1)
    assert defaults.outage is None and defaults.loss_probability is None


@pytest.mark.parametrize(
    "changes",
    [
        {"start": 0},
        {"start": 1.5},
        {"end": 2},
        {"probability": float("nan")},
        {"scope": "global"},
        {"target": None},
    ],
)
def test_compiled_window_rejects_invalid_runtime_values(changes):
    window = compile_communication_events([event()])[0]
    with pytest.raises(ValueError):
        replace(window, **changes)

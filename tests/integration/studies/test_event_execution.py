"""Real service/ADMM and channel evidence for multi-event execution and rejection."""

import json
from dataclasses import asdict
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from oilfield_energy.communication import SimulatedCommunicationChannel
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import CommunicationConfig
from oilfield_energy.modules.studies.api import compile_communication_events
from oilfield_energy.modules.studies.contracts import ScenarioEvent
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation


def event(
    identity, target="SC", start=3, end=8, kind="communication_outage", magnitude=1.0
):
    return ScenarioEvent.model_validate(
        dict(
            event_id=identity,
            target=target,
            start=start,
            end=end,
            event_type=kind,
            magnitude=magnitude,
            time_axis="coordination_iteration"
            if kind.startswith("communication")
            else "clock_minute",
        )
    )


def request(events, **changes):
    return SimulationRequest.model_validate(
        dict(
            scenario_type="communication_fault",
            steps=4,
            events=events,
            communication_loss_probability=0,
            communication_max_delay_iterations=0,
            communication_outage_start_iteration=1000,
            communication_outage_end_iteration=1000,
        )
        | changes
    )


@pytest.fixture(scope="module")
def actual_runs():
    a, b = event("a"), event("b", target="YA_B", start=12, end=16)
    return {
        "ab": run_simulation(request([a, b])),
        "ba": run_simulation(request([b, a])),
        "late": run_simulation(request([event("later", start=100, end=110)])),
    }


def test_real_service_executes_both_faults_and_is_permutation_invariant(actual_runs):
    ab, ba = actual_runs["ab"], actual_runs["ba"]
    assert ab.executive_summary.overall_passed and ba.executive_summary.overall_passed
    rows = ab.communication.event_executions
    assert [row.window.event_id for row in rows] == ["a", "b"]
    assert [row.status for row in rows] == ["executed", "executed"]
    assert [row.dropped_while_active for row in rows] == [12, 10]
    assert rows[0].observed_ticks == tuple(range(3, 9))
    assert rows[1].observed_ticks == tuple(range(12, 17))
    assert ab.communication.outage_dropped == 22
    assert ab.communication == ba.communication
    assert ab.admm_history == ba.admm_history
    assert ab.cluster_timeseries == ba.cluster_timeseries
    assert ab.communication.outage_region is None  # No misleading singleton projection.
    json.dumps(ab.model_dump(mode="json"), allow_nan=False)


def test_convergence_cannot_certify_unreached_fault_window(actual_runs):
    result = actual_runs["late"]
    record = result.communication.event_executions[0]
    assert record.status == "not_reached" and record.observed_ticks == ()
    assert record.dropped_while_active == 0
    assert not result.executive_summary.overall_passed
    item = next(
        item
        for item in result.validation_items
        if item.code == "COMMUNICATION_EVENTS_COVERED"
    )
    assert not item.passed
    assert next(
        item for item in result.validation_items if item.code == "ADMM_CONVERGENCE"
    ).passed


def test_historical_result_does_not_invent_event_execution_records(actual_runs):
    payload = actual_runs["ab"].model_dump(mode="json")
    payload["metadata"]["schema_version"] = "1.2.0"
    payload["communication"].pop("event_executions")
    payload["validation_items"] = [
        item
        for item in payload["validation_items"]
        if item["code"] != "COMMUNICATION_EVENTS_COVERED"
    ]
    restored = SimulationResult.model_validate(payload)
    assert restored.communication.event_executions == ()
    assert restored.metadata.schema_version == "1.2.0"


def test_real_service_applies_wind_surge_to_single_microgrid():
    wind_surge = event(
        "wind-surge",
        start=0,
        end=180,
        kind="wind_surge",
        magnitude=1.35,
    )
    result = run_simulation(request([wind_surge], scenario_type="single_microgrid"))
    source = build_synthetic_case(steps=4).microgrids[0]

    expected_during_event = sum(
        min(values[0] * wind_surge.magnitude, source.wind_capacity_mw[bus])
        for bus, values in source.wind_available_mw.items()
    )
    expected_after_event = sum(
        values[1] for values in source.wind_available_mw.values()
    )

    assert result.request.events == [wind_surge]
    assert result.timeseries[0].wind_available_mw == pytest.approx(
        expected_during_event
    )
    assert result.timeseries[1].wind_available_mw == pytest.approx(expected_after_event)


@pytest.mark.parametrize("kind", ["inapplicable", "fractional"])
def test_old_request_evidence_remains_readable_but_cannot_be_reexecuted(
    actual_runs, kind
):
    payload = actual_runs["ab"].model_dump(mode="json")
    payload["metadata"]["schema_version"] = "1.2.0"
    payload["communication"].pop("event_executions")
    if kind == "inapplicable":
        payload["request"]["scenario_type"] = "cluster_coordination"
    else:
        payload["request"]["events"][0]["start"] = 0.5
    restored = SimulationResult.model_validate(payload)
    assert restored.request.model_dump(mode="json") == payload["request"]
    assert restored.communication.event_executions == ()
    with patch(
        "oilfield_energy.service._solve",
        side_effect=AssertionError("must reject before solve"),
    ):
        with pytest.raises(ValidationError):
            run_simulation(restored.request)


@pytest.mark.parametrize(
    "scenario,entry",
    [
        ("cluster_coordination", event("fault")),
        ("single_microgrid", event("fault")),
        (
            "group_control",
            event("load", kind="load_drop", start=0, end=1440, magnitude=0.5),
        ),
        (
            "single_microgrid",
            event("other", target="YA_B", kind="pv_surge", start=0, end=60),
        ),
    ],
)
def test_unsupported_request_fails_before_solver_including_unvalidated_copy(
    scenario, entry
):
    with patch(
        "oilfield_energy.service._solve",
        side_effect=AssertionError("solver must not run"),
    ):
        with pytest.raises(ValidationError):
            request([entry], scenario_type=scenario)
        unvalidated = request([]).model_copy(
            update={
                "events": [entry],
                "scenario_type": type(request([]).scenario_type)(scenario),
            }
        )
        with pytest.raises(ValueError):
            run_simulation(unvalidated)


def test_channel_preserves_all_windows_and_counts_overlaps_without_double_dropping():
    windows = compile_communication_events(
        [
            event("loss1", start=2, end=4, kind="communication_packet_loss"),
            event("loss2", start=4, end=6, kind="communication_packet_loss"),
            event("outage", start=3, end=5),
            event("partial", start=6, end=9),
            event("future", start=10, end=11),
        ]
    )
    channel = SimulatedCommunicationChannel(CommunicationConfig(events=windows))
    results = []
    for tick in range(1, 8):
        for region in ("SC", "YA_B", "YA_C"):
            results.append(channel.send("coordinator", region, tick, tick))
    rows = {item.window.event_id: item for item in channel.event_executions()}
    assert (
        rows["loss1"].matched_messages == 9 and rows["loss1"].dropped_while_active == 9
    )
    assert (
        rows["loss2"].matched_messages == 9 and rows["loss2"].dropped_while_active == 9
    )
    assert rows["outage"].matched_messages == 3
    assert rows["partial"].status == "partially_executed"
    assert rows["future"].status == "not_reached"
    assert channel.metrics.dropped == 16  # global ticks 2..6 (15) plus SC tick 7.
    assert sum(not accepted for accepted in results) == channel.metrics.dropped
    # Legacy dataclass JSON exporters continue to work with nonempty evidence.
    channel.metrics.event_executions = channel.event_executions()
    json.dumps(asdict(channel.metrics), allow_nan=False)


def test_legacy_single_window_and_new_explicit_window_preserve_seeded_message_behavior():
    old = SimulatedCommunicationChannel(
        CommunicationConfig(
            loss_probability=0.35,
            loss_start_iteration=3,
            loss_end_iteration=8,
            max_delay_iterations=3,
        )
    )
    new = SimulatedCommunicationChannel(
        CommunicationConfig(
            events=compile_communication_events(
                [event("loss", kind="communication_packet_loss", magnitude=0.35)]
            ),
            max_delay_iterations=3,
        )
    )
    for tick in range(1, 20):
        for region in ("SC", "YA_B", "YA_C"):
            assert old.send("coordinator", region, tick, tick) == new.send(
                "coordinator", region, tick, tick
            )
            assert old.receive(region, tick) == new.receive(region, tick)
    assert asdict(old.metrics) == asdict(new.metrics)

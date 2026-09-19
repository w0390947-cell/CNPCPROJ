"""Independent conservation and observed-response checks for control contracts."""

from dataclasses import FrozenInstanceError, replace
from math import fsum
from random import Random

import pytest

from oilfield_energy.modules.control.api import (
    CurtailmentLedger,
    RestorationMonitor,
    disaggregate_executed_generation,
    limit_synthetic_generation,
)
from oilfield_energy.modules.control.contracts import (
    RestorationCommand,
    RestorationObservation,
    RestorationStation,
)


def observation(t, power=1.0, *, available=2.0, baseline=2.0, caps=(), ack="restore-1", valid=True):
    return RestorationObservation(
        t, (RestorationStation("PV", power, baseline, available, caps),), valid, ack
    )


def monitor():
    subject = RestorationMonitor(dwell_minutes=3.0, interval_minutes=1.0)
    subject.begin(RestorationCommand("restore-1", 0.1, 0.1, observation(0)))
    return subject


def test_only_synthetic_execution_can_apply_physical_ceiling():
    assert limit_synthetic_generation(7.0, 1.0, 5.0) == 1.0
    assert limit_synthetic_generation(7.0, 9.0, 5.0) == 5.0
    assert limit_synthetic_generation(7.0, 0.0, 5.0) == 0.0
    with pytest.raises(ValueError, match="exceeds physical"):
        disaggregate_executed_generation(7.0, {"W": 7.0}, {"W": 1.0})


def test_weighted_saturation_has_analytic_answer_and_rejects_wrong_ids():
    values = disaggregate_executed_generation(6.0, {"A": 3.0, "B": 1.0}, {"B": 8.0, "A": 2.0})
    assert {s.resource_id: s.actual_mw for s in values} == {"A": 2.0, "B": 4.0}
    with pytest.raises(ValueError, match="IDs"):
        disaggregate_executed_generation(1.0, {"A": 1.0}, {"B": 1.0})


def test_generation_properties_over_many_saturation_patterns():
    rng = Random(20260917)
    for _ in range(200):
        ids = tuple(str(i) for i in range(rng.randrange(1, 9)))
        upper = {sid: rng.random() * 10 if rng.random() > 0.3 else 0.0 for sid in ids}
        reference = {sid: rng.random() * 4 for sid in ids}
        total = rng.random() * fsum(upper.values())
        values = disaggregate_executed_generation(total, reference, upper)
        assert abs(fsum(s.actual_mw for s in values) - total) <= 1e-9
        assert all(0 <= s.actual_mw <= upper[s.resource_id] + 1e-12 for s in values)
        reversed_values = disaggregate_executed_generation(
            total, dict(reversed(reference.items())), upper
        )
        assert {s.resource_id: s.actual_mw for s in values} == pytest.approx(
            {s.resource_id: s.actual_mw for s in reversed_values}, abs=1e-9
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_bad_generation_is_rejected(bad):
    with pytest.raises(ValueError):
        limit_synthetic_generation(bad, 2.0, 3.0)
    with pytest.raises(ValueError):
        disaggregate_executed_generation(1.0, {"A": 1.0}, {"A": bad})


def test_real_output_change_is_distinct_from_cap_release_and_ack():
    subject = monitor()
    assert subject.observe(observation(1)).status == "pending"
    assert subject.observe(observation(2)).achieved_mw is None
    evidence = subject.observe(observation(3, power=0.98))
    assert evidence.status == "observed"
    assert evidence.cap_released_mw == 0.1
    assert evidence.measured_delta_mw == pytest.approx(-0.02)
    assert evidence.achieved_mw == 0.0
    with pytest.raises(FrozenInstanceError):
        evidence.reason = "fake success"


@pytest.mark.parametrize(
    "change",
    [
        dict(available=1.5),
        dict(baseline=1.5),
        dict(caps=(("hard", 1.0),)),
        dict(ack="wrong-command"),
        dict(valid=False),
    ],
)
def test_temporary_ambiguity_is_latched_even_if_context_returns(change):
    subject = monitor()
    assert subject.observe(observation(1, **change)).status == "unknown"
    subject.observe(observation(2))
    evidence = subject.observe(observation(3, power=1.1))
    assert evidence.status == "unknown"
    assert evidence.measured_delta_mw is None and evidence.achieved_mw is None
    subject.cancel()
    assert subject.observe(observation(4)) is None


def test_gap_replay_and_unsent_cap_cannot_confirm_execution():
    for second in (0.0, 2.0):
        assert monitor().observe(observation(second)).status == "unknown"
    subject = monitor()
    subject.begin(RestorationCommand("restore-1", 0.1, 0.0, observation(0)))
    assert subject.observe(observation(1)).reason == "cap_release_does_not_match_request"


def test_ledger_keeps_ownership_when_weather_or_plan_drops():
    ledger = CurtailmentLedger()
    ledger.synchronize({"PV": 2.0}, {"PV": 1.0})
    ledger.synchronize({"PV": 0.5}, {"PV": 1.0})
    assert ledger.remaining_mw({"PV": 1.0}) == 1.0
    copied = ledger.references()
    copied["PV"] = 0.0
    assert ledger.references() == {"PV": 2.0}
    ledger.synchronize({"PV": 0.5}, {})
    assert ledger.remaining_mw({}) == 0.0


def test_runtime_mutable_context_is_rejected():
    with pytest.raises(ValueError):
        replace(observation(0), stations=list(observation(0).stations))

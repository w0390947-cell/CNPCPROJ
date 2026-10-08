"""Recursive safety is tested independently of economic optimization success."""

import pytest

from oilfield_energy.modules.control.api import tube_contains
from oilfield_energy.modules.control.contracts import TubeCommand
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from oilfield_energy.workflows.cluster_execution.api import ClusterSafetyController
from oilfield_energy.workflows.cluster_execution.api import replay_cluster_safety
from oilfield_energy.workflows.cluster_execution.contracts import ClusterSafetyEvidence
from tests.support.cluster_safety import safety_domain, observed


def test_failed_candidates_keep_the_same_certified_tail_and_original_deadline():
    d = safety_domain(0.5)
    c = ClusterSafetyController(d)
    initial = c.plan
    deadline = {key: t.deadline_minute for key, t in c.tasks.items()}
    bad = tuple(
        TubeCommand(
            p_mw=tuple(99.0 for _ in r.devices.resources), q_mvar=r.initial.q_mvar
        )
        for r in d.regions
    )
    for k in range(100):
        step = c.advance(
            observed(d, k, upper=k % 2 == 0),
            bad if k % 5 == 0 else None,
            "optimizer unavailable",
        )
        assert step.source in {"backup", "terminal"}
        assert all(p.certified for p in step.path.networks)
        assert c.plan == initial
        assert all(t.deadline_minute == deadline[key] for key, t in c.tasks.items())
        if k == 14:
            assert all(
                t.status == "recovered" and t.completed_minute <= 15
                for t in c.tasks.values()
            )
    assert c.reserve_call_ready
    assert all(t.deadline_status == "met" for t in c.tasks.values())
    assert len(c.plans) == 1 and len(c.steps) == 100


def test_out_of_domain_input_is_rejected_before_any_state_change():
    d = safety_domain()
    c = ClusterSafetyController(d)
    rows = list(observed(d))
    f = rows[0]
    p = list(f.load_p_mw)
    p[1] = PowerInterval(low=999, high=999)
    rows[0] = f.model_copy(update={"load_p_mw": tuple(p)})
    before = c.states
    with pytest.raises(ValueError, match="declared load domain"):
        c.advance(tuple(rows), None)
    assert c.minute == 0 and c.states == before and not c.steps


def test_variable_simultaneous_reserve_call_then_return_before_readmission():
    d = safety_domain()
    c = ClusterSafetyController(d)
    event = c.accept_reserve_event()
    with pytest.raises(ValueError, match="locked"):
        c.accept_reserve_event()
    with pytest.raises(ValueError, match="amplitude/slew"):
        c.advance(
            observed(d),
            None,
            reserve_requests=tuple(2 * r.required_reserve_mw for r in d.regions),
        )
    assert c.minute == 0
    pattern = (0.0, 1.0, 0.0, -1.0, 0.0)
    for k in range(event.end_minute):
        requests = tuple(
            pattern[k % len(pattern)]
            * r.required_reserve_mw
            * (1 if i % 2 == 0 else -1)
            for i, r in enumerate(d.regions)
        )
        step = c.advance(
            observed(d, k, upper=k % 2 == 0), None, reserve_requests=requests
        )
        assert step.source == "reserve" and all(x.certified for x in step.path.networks)
        assert all(
            tube_contains(b, s)
            for b, s in zip(event.successor.initial, step.after, strict=True)
        )
        assert not c.reserve_call_ready
    assert c.plan == event.successor
    while not c.reserve_call_ready:
        c.advance(observed(d, c.minute), None)
        assert c.minute < 120
    second = c.accept_reserve_event()
    assert second.number == 2 and second.start_minute > event.end_minute
    assert second.certificate.start_minute == c.minute
    assert (
        replay_cluster_safety(
            ClusterSafetyEvidence.model_validate_json(c.evidence().model_dump_json())
        )
        == c.minute
    )


def test_replay_rejects_a_modified_actual_state():
    d = safety_domain(reserve=False)
    c = ClusterSafetyController(d)
    c.advance(observed(d), None)
    raw = c.evidence().model_dump(mode="json")
    raw["steps"][0]["after"][0]["energy_mwh"] += 0.01
    with pytest.raises(ValueError, match="replay differs"):
        replay_cluster_safety(ClusterSafetyEvidence.model_validate(raw))


def test_recoverable_future_firm_drop_does_not_abort_an_already_certified_tail():
    d = safety_domain()
    regions = []
    targets = []
    for r in d.regions:
        wind = next(i for i, x in enumerate(r.devices.resources) if x.kind == "wind")
        forecasts = []
        for k, f in enumerate(r.forecasts):
            a = list(f.available_mw)
            if k >= 15:
                a[wind] = 0.0
            forecasts.append(f.model_copy(update={"available_mw": tuple(a)}))
        regions.append(r.model_copy(update={"forecasts": tuple(forecasts)}))
        p = list(r.initial.p_mw)
        p[wind] = 0.02
        targets.append(TubeCommand(p_mw=tuple(p), q_mvar=r.initial.q_mvar))
    d = d.model_copy(update={"regions": tuple(regions)})
    c = ClusterSafetyController(d)
    s = c.advance(observed(d), c.commands_for_endpoints(tuple(targets)))
    assert s.source == "economic" and s.reserve.status == "unknown"
    assert all(
        t.first_minute == 1 and t.deadline_minute == 16 for t in c.tasks.values()
    )
    for k in range(1, 20):
        c.advance(observed(d, k), None)
    assert c.minute == 20 and all(t.status == "recovered" for t in c.tasks.values())
    assert all(t.deadline_minute == 16 for t in c.tasks.values())


def test_individually_safe_regions_cannot_authorize_joint_overload():
    d = safety_domain(reserve=False)
    # Every region imports approximately 2 MW, individually below 10 MW.
    d = d.model_copy(update={"import_limit_mw": 5.9})
    with pytest.raises(ValueError, match="joint continuous import capacity"):
        ClusterSafetyController(d)


def test_successful_economic_command_is_also_required_to_have_a_successor():
    d = safety_domain(reserve=False)
    c = ClusterSafetyController(d)
    proposed = []
    for r in d.regions:
        p = list(r.initial.p_mw)
        i = next(i for i, x in enumerate(r.devices.resources) if x.kind == "storage")
        p[i] = 0.1
        proposed.append(TubeCommand(p_mw=tuple(p), q_mvar=r.initial.q_mvar))
    step = c.advance(observed(d), tuple(proposed))
    assert step.source == "economic" and len(c.plans) == 2
    successor = c.plan
    for _ in range(95):
        c.advance(observed(d, c.minute), None)
    assert c.plan == successor and c.steps[-1].source == "terminal"

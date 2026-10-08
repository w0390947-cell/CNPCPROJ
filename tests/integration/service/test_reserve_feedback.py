"""V6 feedback uses actual execution and the full product, never a legacy flag."""

from dataclasses import replace

import pytest

from oilfield_energy.bootstrap.adapters import cluster_rolling
from oilfield_energy.bootstrap.adapters.cluster_execution import execution_inputs
from oilfield_energy.bootstrap.adapters.cluster_safety import ClusterSafetyPort
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy
from oilfield_energy.modules.dispatch.contracts import (
    StorageReservePolicy,
    ReserveDeliveryPolicy,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    ClusterSafetyPolicy,
)
from tests.support.reserve_case import stationary_reserve_case


def port(energy=2.0):
    case = stationary_reserve_case()
    case = replace(
        case,
        microgrids=[
            replace(m, storage=replace(m.storage, e_initial_mwh=energy))
            for m in case.microgrids
        ],
    )
    _, plants, _ = execution_inputs(case, packaged=False, events=[])
    policy = ExecutionPolicy(
        cluster_safety=ClusterSafetyPolicy(),
        dynamic_tracking=DynamicTrackingPolicy(),
        storage_reserve=StorageReservePolicy(),
        reserve_delivery=ReserveDeliveryPolicy(),
    )
    return ClusterSafetyPort(case, policy, True, plants)


@pytest.mark.parametrize("energy", [0.5, 2.0])
def test_actual_feedback_preserves_original_deadlines_without_legacy_observation(
    monkeypatch, energy
):
    def forbidden(**kwargs):
        raise AssertionError("legacy constant-power observer is not v6 admission")

    monkeypatch.setattr(cluster_rolling, "observe_storage_reserve", forbidden)
    p = port(energy)
    for _ in range(20):
        p.advance("economic optimizer unavailable")
    assert all(c.minute == 20 for c in p.checkpoints().values())
    assert all(
        c.status == "certified"
        for c in p.controller.observations
        if c.start_minute >= 15
    )
    if energy == 0.5:
        assert p.controller.task_history
        assert all(t.deadline_minute == 15 for t in p.controller.task_history)
        assert all(
            t.status == "recovered" and t.completed_minute <= 15
            for t in p.controller.tasks.values()
        )
    evidence = p.minute_evidence(p.domain.regions[0].region, 20)
    assert not evidence.p_target_mw  # No manufactured zero-error economic target.
    assert (
        next(c for c in evidence.checks if c.code == "CLUSTER_INTERVAL_SAFETY").status
        == "passed"
    )
    assert (
        next(c for c in evidence.checks if c.code == "ECONOMIC_TRACKING").status
        == "unknown"
    )


def test_explicit_calls_use_the_same_formal_port_and_actual_state():
    p = port()
    event = p.controller.accept_reserve_event()
    for minute in range(event.end_minute):
        p.advance(
            reserve_requests=tuple(r.required_reserve_mw for r in p.domain.regions)
        )
    assert len(p.controller.steps) == 16
    assert all(s.source == "reserve" for s in p.controller.steps)
    assert len(p.outputs[p.domain.regions[0].region]) == 16
    assert not p.controller.reserve_call_ready
    with pytest.raises(ValueError, match="locked"):
        p.controller.accept_reserve_event()

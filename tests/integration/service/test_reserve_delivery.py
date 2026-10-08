"""Real default entry point must produce or refuse the new reserve evidence."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation
from tests.support.reserve_case import stationary_reserve_case


def test_default_entry_preserves_reserve_rejection_and_serializes_evidence():
    case = build_synthetic_case(4)
    case = replace(
        case,
        time_hours=np.arange(4) / 4,
        assumptions=replace(case.assumptions, dt_hours=0.25),
    )
    result = run_simulation(
        SimulationRequest(scenario_type="cluster_coordination", steps=4),
        input_case=case,
    )
    execution = result.cluster_execution
    assert execution is not None and execution.algorithm_version == "cluster-rolling-v6"
    assert execution.policy.reserve_delivery.response_minutes == 1
    assert execution.rolling_updates, execution.model_dump_json()
    first = execution.rolling_updates[0]
    assert not first.adopted and not first.actual_end_energy_mwh
    assert execution.safety is not None
    assert execution.safety.initial_continuation is None
    assert execution.safety.failure and not execution.safety.steps
    assert (
        SimulationResult.model_validate_json(result.model_dump_json()).cluster_execution
        == execution
    )


@pytest.mark.parametrize("initial_energy", [2.0, 0.5])
def test_constant_three_region_case_adopts_only_certified_service(initial_energy):
    case = stationary_reserve_case()
    case = replace(
        case,
        microgrids=[
            replace(mg, storage=replace(mg.storage, e_initial_mwh=initial_energy))
            for mg in case.microgrids
        ],
    )
    result = run_simulation(
        SimulationRequest(scenario_type="cluster_coordination", steps=4),
        input_case=case,
    )
    execution = result.cluster_execution
    assert execution is not None
    assert (
        execution.rolling_updates
        and execution.rolling_updates[0].continuation_available
    ), [(u.start_minute, u.reason) for u in execution.rolling_updates]
    assert execution.reserve_observations
    assert any(c.status == "certified" for c in execution.reserve_observations)
    assert all(u.actual_end_energy_mwh for u in execution.rolling_updates), [
        (u.start_minute, u.reason) for u in execution.rolling_updates
    ]
    if initial_energy == 0.5:
        first_tasks = [
            t for t in execution.reserve_recovery_history if t.first_minute == 0
        ]
        assert first_tasks and all(t.deadline_minute == 15 for t in first_tasks)
        assert any(
            t.status == "recovered" and t.completed_minute <= 15 for t in first_tasks
        )
    assert execution.safety is not None and len(execution.safety.steps) == 60
    assert execution.safety.initial_continuation is not None
    assert all(u.continuation_available for u in execution.rolling_updates)
    for step in execution.safety.steps:
        assert all(n.certified for n in step.path.networks)
        assert step.reserve is not None
        assert step.reserve.status == "certified" or step.minute + 1 < 15

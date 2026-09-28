"""The automated executor never adopts an otherwise valid out-of-band plan."""

from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters import cluster_execution as engines
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


@pytest.mark.parametrize("code", ["P_TRACKING", "Q_TRACKING"])
def test_failed_tracking_in_second_window_stops_adoption_and_preserves_prefix(code):
    original = engines.ExecutionEngines.solve_windows
    count = 0

    def solve_windows(self, *args, **kwargs):
        nonlocal count
        count += 1
        results = original(self, *args, **kwargs)
        region, plan = results["SC"]
        if count == 2:
            assert plan is not None and region.status == "passed"
            region = region.model_copy(
                update={
                    "status": "violated",
                    "checks": tuple(
                        c.model_copy(update={"status": "violated", "actual": 0.08})
                        if c.code == code
                        else c
                        for c in region.checks
                    ),
                }
            )
            results["SC"] = region, plan
        return results

    case = build_synthetic_case(4)
    case = replace(
        case,
        time_hours=np.arange(4) / 4,
        assumptions=replace(case.assumptions, dt_hours=0.25),
    )
    with patch.object(engines.ExecutionEngines, "solve_windows", solve_windows):
        result = run_simulation(
            SimulationRequest(scenario_type="cluster_coordination", steps=4),
            input_case=case,
        )
    execution = result.cluster_execution
    assert len(execution.rolling_updates) == 2
    assert execution.rolling_updates[0].adopted
    rejected = execution.rolling_updates[1]
    assert not rejected.adopted and rejected.status == "violated"
    failed = next(c for c in rejected.regional_checks["SC"] if c.code == code)
    assert failed.status == "violated" and failed.actual == 0.08
    for region in execution.stages[2].regions:
        assert len(region.p_mw) == 15 and region.time_minutes[-1] == 14
        assert (
            region.storage_energy_mwh[-1] == rejected.initial_energy_mwh[region.region]
        )


def test_checker_uses_engineering_limits_and_separate_roundoff_allowance():
    policy = ExecutionPolicy(
        p_tracking_tolerance_mw=0.02, q_tracking_tolerance_mvar=0.03
    )
    checks = engines._tracking(
        np.array([0.02000001]),
        np.array([0.030002]),
        np.zeros(1),
        np.zeros(1),
        policy,
    )
    assert checks[0].status == "passed" and checks[0].limit == 0.02
    assert checks[1].status == "violated" and checks[1].limit == 0.03

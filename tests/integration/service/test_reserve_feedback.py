"""Exercise actual short-window coordination and prospective command adoption."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters import cluster_rolling
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation


@pytest.mark.parametrize("rejected", [False, True])
def test_minute_feedback_replans_only_unexecuted_targets_and_preserves_failure(
    monkeypatch, rejected
):
    case = build_synthetic_case(4)
    case = replace(
        case,
        time_hours=np.arange(4) / 4,
        assumptions=replace(case.assumptions, dt_hours=0.25),
    )
    observed = cluster_rolling.observe_storage_reserve
    coordinate = cluster_rolling.run_admm_coordination
    horizons = []

    def trigger(**kwargs):
        row = observed(**kwargs)
        # Force a single feedback event; reserve arithmetic is tested separately.
        return row.model_copy(
            update={"deficient": row.minute == 7 and row.region == "SC"}
        )

    def replan(window, *args, **kwargs):
        if window.assumptions.dt_hours == 1 / 60:
            horizons.append(tuple(window.time_hours * 60))
            if rejected:
                raise RuntimeError("injected minute reserve infeasibility")
        return coordinate(window, *args, **kwargs)

    monkeypatch.setattr(cluster_rolling, "observe_storage_reserve", trigger)
    monkeypatch.setattr(cluster_rolling, "run_admm_coordination", replan)
    result = run_simulation(
        SimulationRequest(scenario_type="cluster_coordination", steps=4),
        input_case=case,
    )
    execution = result.cluster_execution
    assert execution is not None
    assert horizons == [tuple(range(7, 15))]
    assert len(execution.reserve_target_adjustments) == 1
    change = execution.reserve_target_adjustments[0]
    assert change.minute == 7 and change.trigger_regions == ("SC",)
    assert change.update.adopted is not rejected
    assert len(change.original_p_mw["SC"]) == 8
    first = execution.rolling_updates[0]
    for region in execution.stages[2].regions:
        np.testing.assert_allclose(
            region.p_target_mw[:7], first.adopted_p_mw[region.region], atol=1e-10
        )
        if rejected:
            assert len(region.p_mw) == 7
            assert not first.actual_end_energy_mwh
            assert "injected minute" in first.reason
        else:
            assert len(region.p_mw) == 60
            np.testing.assert_array_equal(
                region.p_target_mw[7:15], change.adopted_p_mw[region.region]
            )
            assert all(
                check.status == "passed"
                for check in change.update.regional_checks[region.region]
            )
    if not rejected:
        assert (
            max(
                sum(values[t] for values in change.adopted_p_mw.values())
                for t in range(8)
            )
            <= case.cluster_import_limit_mw + 1e-6
        )
        assert all(row.actual_end_energy_mwh for row in execution.rolling_updates)
    assert (
        SimulationResult.model_validate_json(result.model_dump_json()).cluster_execution
        == execution
    )

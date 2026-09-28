"""Managed single-region quality, independent physical checks and compatibility."""

import numpy as np
import pytest

from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation


@pytest.mark.parametrize(
    "region,steps,storage",
    [("SC", 8, True), ("SC", 96, True), ("YA_B", 24, False), ("YA_C", 24, True)],
)
def test_single_quality_uses_backend_budgets_and_preserves_sufficient_budget_results(
    region, steps, storage
):
    request = SimulationRequest(
        region=region,
        steps=steps,
        storage_enabled=storage,
        solver={"time_limit_seconds": 180},
    )
    manual = run_simulation(request)
    managed = run_simulation(
        SimulationRequest.model_validate(
            {
                **request.model_dump(),
                "solver": {
                    "quality_policy": "quality-first-v1",
                    "time_limit_seconds": 0.001,
                },
            }
        )
    )
    assert managed.executive_summary.overall_passed
    assert managed.cluster_execution is None
    quality = managed.computation_quality
    assert quality is not None and not quality.reference_coordination
    assert set(quality.reference_optimizations) == {"baseline", "optimized"}
    for records in quality.reference_optimizations.values():
        assert records[-1].status == "satisfied"
        assert records[0].attempts[0].budget_seconds == 180
    assert managed.metadata.steps == steps and managed.metadata.region == region
    np.testing.assert_allclose(
        [p.p_grid_optimized_mw for p in managed.timeseries],
        [p.p_grid_optimized_mw for p in manual.timeseries],
        atol=1e-6,
    )
    old = manual.model_dump(mode="json")
    old.pop("computation_quality")
    old["request"]["solver"].pop("quality_policy")
    restored = SimulationResult.model_validate(old)
    assert restored.computation_quality is None
    assert restored.request.solver.quality_policy is None


def test_feasible_single_result_with_insufficient_quality_is_preserved_but_not_passed(
    monkeypatch,
):
    import oilfield_energy.service as service

    original = service._solve

    def limited(*args, **kwargs):
        result, details = original(*args, **kwargs)
        if kwargs["storage_enabled"]:
            quality = details["optimization_quality"][-1]
            quality["status"] = "budget_exhausted"
            quality["attempts"][-1]["relative_gap"] = 0.2
        return result, details

    monkeypatch.setattr(service, "_solve", limited)
    result = run_simulation(
        SimulationRequest(steps=4, solver={"quality_policy": "quality-first-v1"})
    )
    assert result.timeseries and not result.executive_summary.overall_passed
    check = next(
        c
        for c in result.validation_items
        if c.code == "REFERENCE_OPTIMIZATION_QUALITY" and c.scope == "optimized"
    )
    assert check.assessment_status == "unknown" and not check.passed
    assert (
        result.computation_quality.reference_optimizations["optimized"][-1].status
        == "budget_exhausted"
    )


@pytest.mark.parametrize("scenario", ["communication_fault", "group_control"])
def test_other_research_scenarios_cannot_silently_enable_this_policy(scenario):
    with pytest.raises(ValueError, match="quality-first"):
        SimulationRequest(
            scenario_type=scenario, solver={"quality_policy": "quality-first-v1"}
        )

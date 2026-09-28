"""Synthetic SDK checks: no fixture files, source path injection or field data."""

import json
from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.model import solve_case
from oilfield_energy.regional_control import RegionalConvexController
from oilfield_energy.service import ScenarioType, SimulationRequest, run_simulation
from tests.legacy_case_fixture import build_synthetic_case


@pytest.mark.parametrize("backend", ["misocp", "legacy_milp", "convex"])
@pytest.mark.parametrize("kind", ["wind", "pv"])
def test_nameplate_excess_does_not_change_cost_or_dispatch(backend, kind):
    case = build_synthetic_case(steps=8)
    original = case.microgrids[0]
    ratings = getattr(original, f"{kind}_capacity_mw")
    bus = next(iter(ratings))
    costs, objectives, profiles = [], [], []
    for factor in (1.0, 2.0):
        mg = replace(
            original,
            p_grid_min_mw=0.5,
            **{f"{kind}_available_mw": {bus: np.full(8, ratings[bus] * factor)}},
        )
        scenario = replace(case, microgrids=[mg])
        floor = np.full(8, 0.5)
        if backend == "convex":
            result = RegionalConvexController(
                scenario, mg, ADMMConfig(), p_grid_security_floor_mw=floor
            ).solve(None)
            costs.append(result.economic_cost.economic_cost_cny)
            objectives.append(result.local_objective_cny)
            profiles.append(result.p_grid_mw)
            extra = result.renewable_accounting.nameplate_excess_mw
        else:
            args = dict(
                storage_enabled=False,
                cluster_coordination=False,
                time_limit_seconds=30,
            )
            if backend == "misocp":
                checked = solve_case_ac_consistent(
                    scenario,
                    ["SC"],
                    p_grid_security_floors_mw={"SC": floor},
                    **args,
                )
                assert checked.passed
                result = checked.optimization
            else:
                result = solve_case(scenario, ["SC"], **args)
            assert result.success
            values = result.microgrids["SC"]
            costs.append(values["economic_cost_cny"])
            objectives.append(result.objective_cny)
            profiles.append(values["p_grid_mw"])
            extra = values[f"{kind}_nameplate_excess_mw"]
            np.testing.assert_allclose(
                values[f"{kind}_available_mw"], ratings[bus] * factor
            )
            np.testing.assert_allclose(
                values[f"{kind}_dispatchable_available_mw"], ratings[bus]
            )
        np.testing.assert_allclose(extra, ratings[bus] * (factor - 1))
    # Compare economic and feasibility invariants, not a unique optimizer solution.
    assert costs[0] == pytest.approx(costs[1], abs=0.02)
    assert objectives[0] == pytest.approx(objectives[1], abs=0.02)
    np.testing.assert_allclose(profiles[0], profiles[1], atol=2e-4)


def test_real_service_preserves_reference_cost_when_regional_targets_are_infeasible():
    import oilfield_energy.service as service
    from oilfield_energy.bootstrap.adapters import cluster_execution as engines

    captured = {}
    original = service.run_admm_coordination
    solve_region = engines.solve_case_ac_consistent

    def infeasible_shancheng(case, names, **kwargs):
        # Deliberately impossible execution request, not a reliance on the old
        # coordination capability bug. Still exercise the real SCIP failure.
        if names == ["SC"]:
            targets = kwargs["pcc_targets"]
            kwargs["pcc_targets"] = {
                **targets,
                "SC": {**targets["SC"], "p_mw": np.full(len(case.time_hours), -100.0)},
            }
        return solve_region(case, names, **kwargs)

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captured["result"] = result
        captured["case"] = args[0]
        captured["loss"] = kwargs["loss_calibration"]
        return result

    with (
        patch.object(service, "run_admm_coordination", capture),
        patch.object(engines, "solve_case_ac_consistent", infeasible_shancheng),
    ):
        result = run_simulation(
            SimulationRequest(
                scenario_type=ScenarioType.CLUSTER_COORDINATION,
                steps=8,
                admm_max_iterations=220,
            )
        )
    assert result.executive_summary.overall_passed == all(
        i.passed for i in result.validation_items
    )
    assessment = result.reference_economics
    assert assessment is not None and assessment.comparison_status == "comparable"
    # Independent direct arithmetic, not another invocation of the shared rule.
    case = captured["case"]
    a = case.assumptions
    expected = 0.0
    for mg in case.microgrids:
        schedule = captured["result"].schedules[mg.name]
        available = sum(
            np.minimum(v, mg.wind_capacity_mw[b])
            for b, v in mg.wind_available_mw.items()
        )
        available += sum(
            np.minimum(v, mg.pv_capacity_mw[b]) for b, v in mg.pv_available_mw.items()
        )
        expected += a.dt_hours * (
            np.dot(case.price_cny_per_mwh, schedule.p_grid_mw)
            + np.sum(np.maximum(0, available - schedule.renewable_used_mw))
            * a.curtailment_cost_cny_per_mwh
            + np.sum(schedule.storage_charge_mw + schedule.storage_discharge_mw)
            * a.storage_degradation_cny_per_mwh
            + np.sum(captured["loss"][mg.name]["p_loss_mw"]) * a.loss_value_cny_per_mwh
        )
    assert assessment.reference.economic_cost_cny == pytest.approx(expected, abs=0.002)
    assert abs(assessment.surrogate_objective_cny - expected) > 100
    central = result.executive_summary.optimized_economic_cost_cny
    assert assessment.gap_percent == pytest.approx(
        100 * (expected - central) / central, abs=2e-6
    )
    assert (
        result.comparison["distributed_reference_cost_gap_percent"]
        == assessment.gap_percent
    )
    sc = next(r for r in result.cluster_execution.stages[0].regions if r.region == "SC")
    assert sc.status == "violated" and "solver_infeasible" in sc.reason
    assert sc.economic_cost_cny is None
    # Aggregate ADMM economics remain available, but an infeasible realization
    # must not be priced as zero or replaced by the centralized cost.
    assert assessment.realized_regional_economic_cost_cny is None
    assert assessment.realization_status == "unavailable"
    assert not assessment.formal_ten_percent_requirement_certified


def test_unconverged_service_preserves_costs_but_does_not_publish_numeric_gap():
    result = run_simulation(
        SimulationRequest(
            scenario_type=ScenarioType.CLUSTER_COORDINATION,
            steps=4,
            admm_max_iterations=4,
        )
    )
    assert not result.executive_summary.overall_passed
    assert result.reference_economics is not None
    assert result.reference_economics.comparison_status == "not_converged"
    assert result.reference_economics.gap_percent is None
    assert "distributed_reference_cost_gap_percent" not in result.comparison
    json.dumps(result.model_dump(mode="json"), allow_nan=False)

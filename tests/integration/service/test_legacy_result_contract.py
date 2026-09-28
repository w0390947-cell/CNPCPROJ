"""Public historical-model compatibility using real solvers and worker processes."""

import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.job_manager import SimulationJobManager
from oilfield_energy.model import solve_case
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation


@pytest.mark.parametrize("grid_min,margin", [(0.4, 0.6), (1.2, 0.6)])
def test_legacy_metrics_describe_enforced_bounds_and_modeled_power_balance(
    grid_min: float, margin: float
) -> None:
    case = build_synthetic_case(steps=8)
    mg = replace(case.microgrids[0], p_grid_min_mw=grid_min)
    case = replace(
        case,
        microgrids=(mg,),
        assumptions=replace(case.assumptions, no_reverse_margin_mw=margin),
    )
    result = solve_case(case, [mg.name], storage_enabled=True, time_limit_seconds=60)
    assert result.success, result.message
    local = result.microgrids[mg.name]
    floor = np.asarray(local["p_grid_security_floor_mw"])
    np.testing.assert_allclose(floor, max(grid_min, margin), atol=1e-10)
    imports = np.asarray(local["p_grid_mw"])
    assert np.all(imports >= floor - 2e-5)
    np.testing.assert_allclose(local["p_grid_security_headroom_mw"], imports - floor, atol=1e-10)
    # Independent nodal conservation: the legacy Q equation has no series loss.
    shunt = sum(local["fixed_shunt_q_mvar_by_bus"].values(), np.zeros(8))
    net_q = np.sum(local["bus_q_demand_mvar"], axis=0) - shunt
    np.testing.assert_allclose(local["q_grid_mvar"], net_q, atol=2e-5)
    np.testing.assert_allclose(local["reactive_loss_mvar"], 0, atol=1e-10)
    net_p = np.sum(local["bus_p_demand_mw"], axis=0)
    np.testing.assert_allclose(imports - net_p, local["loss_mw"], atol=2e-5)
    assert result.cluster["total_active_loss_mwh"] == pytest.approx(
        np.sum(imports - net_p) * case.assumptions.dt_hours, abs=2e-5
    )


@pytest.mark.parametrize("formulation", ["legacy_milp", "misocp"])
@pytest.mark.parametrize("scenario,steps", [("single_microgrid", 8), ("cluster_coordination", 4)])
def test_public_service_completes_each_declared_model_with_honest_result(
    formulation: str, scenario: str, steps: int
) -> None:
    request = SimulationRequest.model_validate(
        {"scenario_type": scenario, "steps": steps, "solver": {"formulation": formulation}}
    )
    result = run_simulation(request)
    assert result.metadata.formulation.value == formulation
    assert len(result.timeseries) == steps
    assert result.solver["success"]
    payload = json.dumps(result.model_dump(mode="json"), allow_nan=False)
    assert SimulationResult.model_validate_json(payload).model_dump(mode="json") == json.loads(
        payload
    )
    for point in result.timeseries:
        assert point.p_grid_security_headroom_mw == pytest.approx(
            point.p_grid_optimized_mw - point.p_grid_security_floor_mw, abs=2e-5
        )
    if formulation == "legacy_milp":
        details = result.solver["optimized"]
        assert details["security_floor_basis"] == "legacy_pcc_import_lower_bound"
        assert details["reactive_loss_basis"] == "legacy_lindistflow_omits_series_reactive_losses"
        # Completion must not override independent AC/constraint failures.
        assert result.executive_summary.overall_passed == all(
            i.passed for i in result.validation_items
        )
    elif scenario == "single_microgrid":
        assert result.executive_summary.overall_passed
    if scenario == "cluster_coordination":
        assert result.executive_summary.overall_passed == all(i.passed for i in result.validation_items)
        assert result.cluster_execution is not None
        assert len(result.cluster_timeseries) == steps
        assert result.admm_history


def test_actual_legacy_worker_publishes_a_readable_result(tmp_path: Path) -> None:
    manager = SimulationJobManager(tmp_path / "jobs")
    try:
        status = manager.create(
            SimulationRequest.model_validate(
                {
                    "scenario_type": "single_microgrid",
                    "steps": 8,
                    "solver": {"formulation": "legacy_milp"},
                }
            )
        )
        deadline = time.monotonic() + 90
        while status.state.value in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            status = manager.get(status.simulation_id)
        assert status.state.value == "succeeded", status
        assert status.result_available
        result = SimulationResult.model_validate_json(
            manager.result_path(status.simulation_id).read_text(encoding="utf-8")
        )
        assert result.metadata.formulation.value == "legacy_milp"
    finally:
        manager.close()

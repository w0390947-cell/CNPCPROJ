from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from oilfield_energy import admm, cli, service
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.hierarchical import run_hierarchical_control
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig


def test_disabled_storage_reaches_actual_admm_and_scope_is_explicit():
    captured = []
    original = service.run_admm_coordination

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    with patch.object(service, "run_admm_coordination", side_effect=observe):
        result = service.run_simulation(
            service.SimulationRequest(
                scenario_type=service.ScenarioType.CLUSTER_COORDINATION,
                storage_enabled=False,
                steps=8,
            )
        )
    assert result.executive_summary.overall_passed
    assert "分布式执行未校核" in result.executive_summary.headline
    assert result.cluster_validation.scope == "reference_only"
    assert result.cluster_validation.execution_status == "not_computed"
    assert result.coordination_snapshot.capabilities.storage_enabled is False
    for schedule in captured[0].schedules.values():
        assert np.max(np.abs(schedule.storage_charge_mw)) < 1e-8
        assert np.max(np.abs(schedule.storage_discharge_mw)) < 1e-8
        np.testing.assert_allclose(
            schedule.storage_energy_mwh, schedule.storage_energy_mwh[0], atol=1e-8
        )
    assert result.reference_economics.reference.storage_degradation_cost_cny < 1e-7
    assert all(i.validation_basis is not None for i in result.validation_items)
    archived = service.SimulationResult.model_validate_json(result.model_dump_json())
    # Item 09 deliberately separates historical records from executable requests.
    # Pydantic equality includes model type; verify wire data and revalidation.
    assert type(archived.request) is service.SimulationRequestRecord
    assert archived.model_dump(mode="json") == result.model_dump(mode="json")
    assert service.SimulationRequest.model_validate(archived.request.model_dump()) == result.request


def test_delayed_messages_do_not_regress_accepted_epoch_and_certificate_matches():
    accepted_epochs = {}
    original = admm.RegionalConvexController.solve

    def observe(controller, signal, **kwargs):
        if signal is not None:
            previous = accepted_epochs.get(controller.microgrid.name, -1)
            assert signal.iteration >= previous
            accepted_epochs[controller.microgrid.name] = signal.iteration
        return original(controller, signal, **kwargs)

    with patch.object(admm.RegionalConvexController, "solve", new=observe):
        result = admm.run_admm_coordination(
            build_synthetic_case(8),
            admm_config=ADMMConfig(max_iterations=220),
            communication_config=CommunicationConfig(
                max_delay_iterations=4, stale_limit_iterations=5, random_seed=20260830
            ),
        )
    assert result.converged
    assert result.communication.rejected_stale_messages > 0
    snap = result.coordination_snapshot
    assert snap is not None and snap.converged
    assert {p.signal_iteration for p in snap.regions} == {snap.epoch}
    assert {p.signal_iteration for p in result.schedules.values()} == {snap.epoch}
    x = np.array([p.p_grid_mw for p in snap.regions])
    q = np.array([p.q_grid_mvar for p in snap.regions])
    primal = np.sqrt(
        np.sum((x - snap.p_references_mw) ** 2) + np.sum((q - snap.q_references_mvar) ** 2)
    )
    dual = snap.rho * np.sqrt(
        np.sum((np.array(snap.p_references_mw) - snap.previous_p_references_mw) ** 2)
        + np.sum((np.array(snap.q_references_mvar) - snap.previous_q_references_mvar) ** 2)
    )
    assert primal == pytest.approx(result.history[-1].primal_residual, abs=1e-12)
    assert dual == pytest.approx(result.history[-1].dual_residual, abs=1e-12)
    assert sum(p.local_objective_cny for p in snap.regions) == pytest.approx(
        result.history[-1].local_objective_cny
    )
    for plan in snap.regions:
        assert result.schedules[plan.name].economic_cost == plan.economic_cost
    before = snap.regions[0].p_grid_mw
    result.schedules[snap.regions[0].name].p_grid_mw[0] = 999
    assert snap.regions[0].p_grid_mw == before


def test_total_outage_has_no_certified_snapshot():
    result = admm.run_admm_coordination(
        build_synthetic_case(4),
        admm_config=ADMMConfig(max_iterations=8),
        communication_config=CommunicationConfig(loss_probability=1, stale_limit_iterations=2),
    )
    assert not result.converged
    assert result.coordination_snapshot is None
    assert result.communication.fallback_uses > 0


def test_boundary_trajectory_is_preserved_and_fails_cluster_execution():
    case = replace(build_synthetic_case(8), cluster_import_limit_mw=15.6)
    result = run_hierarchical_control(
        case, admm_config=ADMMConfig(max_iterations=220), time_limit_seconds=60
    )
    assert result.admm.converged
    assert result.comparison["regional_device_safety_passed"]
    assert not result.comparison["cluster_import_limit_passed"]
    assert not result.comparison["device_safety_passed"]
    assert not result.comparison["overall_passed"]
    stages = {s.stage: s for s in result.cluster_validation.stages}
    assert stages["day_ahead"].status == "passed"
    assert stages["intraday"].status == stages["minute"].status == "violated"
    actual = sum((t.pcc_actual_mw for t in result.tracking.values()), np.zeros(1440))
    assert stages["minute"].peak_import_mw == pytest.approx(float(np.max(actual)))
    assert stages["minute"].violation_steps == int(np.sum(actual > 15.6 + 1e-5))
    assert stages["minute"].violation_steps > 0


def test_cli_saves_diagnostics_before_refusing_execution_certificate(tmp_path):
    result = SimpleNamespace(comparison={"overall_passed": False})
    with (
        patch.object(cli, "run_hierarchical_control", return_value=result),
        patch.object(cli, "run_group_control_scenarios", return_value={}),
        patch.object(cli, "write_hierarchical_outputs") as write,
        patch.object(cli, "write_group_control_scenario_outputs"),
    ):
        with pytest.raises(RuntimeError, match="not certified"):
            cli.run_hierarchy(4, tmp_path, 30, False)
    write.assert_called_once()

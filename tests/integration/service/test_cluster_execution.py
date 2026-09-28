from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters import cluster_execution as engines
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation


def short_case():
    case = build_synthetic_case(4)
    return replace(
        case,
        time_hours=np.arange(4) / 4,
        assumptions=replace(case.assumptions, dt_hours=0.25),
    )


def test_progress_observation_preserves_real_numerical_results():
    case = short_case()
    request = SimulationRequest(scenario_type="cluster_coordination", steps=4)
    plain = run_simulation(request, input_case=case)
    observations = []
    observed = run_simulation(request, observations.append, input_case=case)
    assert observations[-1].stage.value == "succeeded"
    assert [i.passed for i in observed.validation_items] == [
        i.passed for i in plain.validation_items
    ]
    assert (
        observed.executive_summary.overall_passed
        == plain.executive_summary.overall_passed
    )
    assert (
        observed.cluster_execution is not None and plain.cluster_execution is not None
    )
    for before_stage, after_stage in zip(
        plain.cluster_execution.stages, observed.cluster_execution.stages, strict=True
    ):
        assert before_stage.status == after_stage.status
        for before, after in zip(
            before_stage.regions, after_stage.regions, strict=True
        ):
            assert before.region == after.region
            assert [(c.code, c.status) for c in before.checks] == [
                (c.code, c.status) for c in after.checks
            ]
            # MW/Mvar/MWh: tighter than the existing 1e-6 numerical tolerance.
            for field in (
                "p_mw",
                "q_mvar",
                "p_target_mw",
                "q_target_mvar",
                "storage_energy_mwh",
            ):
                np.testing.assert_allclose(
                    getattr(before, field), getattr(after, field), rtol=0, atol=1e-7
                )


@pytest.mark.parametrize("storage_enabled", [False, True])
def test_real_automatic_pipeline_preserves_evidence_and_storage_authority(
    storage_enabled,
):
    case = short_case()
    before = {
        bus: values.copy() for bus, values in case.microgrids[0].load_p_mw.items()
    }
    progress = []
    result = run_simulation(
        SimulationRequest(
            scenario_type="cluster_coordination",
            steps=4,
            storage_enabled=storage_enabled,
        ),
        progress.append,
        input_case=case,
    )
    execution = result.cluster_execution
    assert execution is not None
    assert execution.policy.active_tracking_version == "active-tracking-allocation-v1"
    assert execution.storage_enabled is storage_enabled
    assert [s.stage for s in execution.stages] == ["day_ahead", "intraday", "minute"]
    assert all(len(s.regions) == 3 for s in execution.stages)
    # Both storage modes now use realizable resource envelopes (ADR 0023).
    # Disabled storage must exercise the same execution/evidence assertions.
    assert execution.stages[0].status == "passed"
    assert all(len(r.p_mw) == 60 for r in execution.stages[2].regions)
    assert result.reference_economics.realization_status == "computed"
    assert (
        result.reference_economics.realized_regional_economic_cost_cny
        == pytest.approx(sum(r.economic_cost_cny for r in execution.stages[0].regions))
    )
    assert any("分钟级" in p.label for p in progress)
    assert result.cluster_validation.scope == "hierarchical_execution"
    assert len(execution.rolling_updates) == 4
    rolling = [p.rolling for p in progress if p.rolling is not None]
    assert [p.completed_windows for p in rolling if p.phase == "completed"] == [
        1,
        2,
        3,
        4,
    ]
    assert {p.total_windows for p in rolling} == {4}
    assert rolling[-1].end_minute == 60
    assert all(w.adopted for w in execution.rolling_updates)
    minute_by_name = {r.region: r for r in execution.stages[2].regions}
    for previous, current in zip(
        execution.rolling_updates, execution.rolling_updates[1:]
    ):
        assert current.initial_energy_mwh == previous.actual_end_energy_mwh
        for name, value in current.initial_energy_mwh.items():
            assert (
                value
                == minute_by_name[name].storage_energy_mwh[current.start_minute - 1]
            )
    for window in execution.rolling_updates:
        for checks in window.regional_checks.values():
            tracking = [c for c in checks if c.code in ("P_TRACKING", "Q_TRACKING")]
            assert len(tracking) == 2 and all(c.status == "passed" for c in tracking)
        for name, target in window.adopted_p_mw.items():
            assert minute_by_name[name].p_target_mw[window.start_minute] == target
    assert result.executive_summary.overall_passed == all(
        i.passed for i in result.validation_items
    )
    for stage in execution.stages:
        for region in stage.regions:
            checks = {c.code: c for c in region.checks}
            raw_max = max(abs(p - q) for p, q in zip(region.p_mw, region.p_target_mw))
            if stage.stage == "minute":
                assert region.dynamic_tracking["p"].raw_max_error == pytest.approx(
                    raw_max
                )
                assert (
                    checks["P_TRACKING"].actual
                    == region.dynamic_tracking["p"].post_deadline_max_error
                )
                assert (
                    checks["P_RESPONSE"].status
                    == region.dynamic_tracking["p"].response_status
                )
            else:
                assert not region.dynamic_tracking
                assert checks["P_TRACKING"].actual == pytest.approx(raw_max)
            assert region.resource_p_mw.keys() == region.resource_q_mvar.keys()
            if not storage_enabled:
                assert max(abs(v) for v in region.storage_power_mw) < 1e-6
                assert (
                    max(region.storage_energy_mwh) - min(region.storage_energy_mwh)
                    < 1e-6
                )
                assert checks["STORAGE_DISABLED"].status == "passed"
        assert stage.cluster_import.peak_import_mw == pytest.approx(
            max(
                sum(r.p_mw[i] for r in stage.regions)
                for i in range(len(stage.regions[0].p_mw))
            )
        )
    for bus, values in before.items():
        np.testing.assert_array_equal(values, case.microgrids[0].load_p_mw[bus])
    assert case.microgrids[0].storage.p_max_mw > 0
    assert (
        SimulationResult.model_validate_json(result.model_dump_json()).model_dump()
        == result.model_dump()
    )


def test_regional_solver_failure_preserves_coordination_and_marks_downstream_unknown():
    with patch.object(
        engines,
        "solve_case_ac_consistent",
        side_effect=RuntimeError("injected regional timeout"),
    ):
        result = run_simulation(
            SimulationRequest(scenario_type="cluster_coordination", steps=4),
            input_case=short_case(),
        )
    assert result.admm_history and result.coordination_snapshot.converged
    assert result.cluster_execution.status == "unknown"
    assert not result.executive_summary.overall_passed
    assert all(
        "timeout" in r.reason for r in result.cluster_execution.stages[0].regions
    )
    assert result.cluster_execution.stages[2].status == "not_computed"
    assert all(r.p_mw == () for s in result.cluster_execution.stages for r in s.regions)


def test_historical_result_does_not_acquire_execution_evidence():
    result = run_simulation(SimulationRequest(steps=4))
    raw = result.model_dump()
    raw.pop("cluster_execution")
    raw["metadata"]["schema_version"] = "1.4.0"
    assert SimulationResult.model_validate(raw).cluster_execution is None


def test_existing_hierarchical_entry_also_keeps_storage_disabled():
    from oilfield_energy.hierarchical import run_hierarchical_control

    case = short_case()
    result = run_hierarchical_control(
        case,
        intraday_input_case=case,
        plant_inputs=engines._persistent_plant(case),
        storage_enabled=False,
    )
    assert result.admm.coordination_snapshot.capabilities.storage_enabled is False
    for tracking in result.tracking.values():
        assert max(abs(v) for v in tracking.storage_actual_mw) < 1e-6
        assert max(abs(v) for v in tracking.storage_reactive_actual_mvar) < 1e-6
        assert np.ptp(tracking.storage_energy_mwh) < 1e-6


@pytest.mark.parametrize("failure", ["coordination", "regional_plan"])
def test_failed_second_rolling_update_keeps_executed_prefix_and_stops(failure):
    from oilfield_energy.bootstrap.adapters import cluster_rolling

    real_coordinate = cluster_rolling.run_admm_coordination
    count = 0

    def coordinate(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2 and failure == "coordination":
            raise RuntimeError("injected rolling timeout")
        return real_coordinate(*args, **kwargs)

    solve = engines.ExecutionEngines.solve_windows
    solve_count = 0

    def solve_region(self, *args, **kwargs):
        nonlocal solve_count
        solve_count += 1
        if failure == "regional_plan" and solve_count == 2:
            raise RuntimeError("injected rolling regional timeout")
        return solve(self, *args, **kwargs)

    with (
        patch.object(cluster_rolling, "run_admm_coordination", side_effect=coordinate),
        patch.object(engines.ExecutionEngines, "solve_windows", new=solve_region),
    ):
        result = run_simulation(
            SimulationRequest(scenario_type="cluster_coordination", steps=4),
            input_case=short_case(),
        )
    execution = result.cluster_execution
    assert count == 2 and len(execution.rolling_updates) == 2
    assert execution.rolling_updates[0].adopted
    assert not execution.rolling_updates[1].adopted
    assert "timeout" in execution.rolling_updates[1].reason
    for region in execution.stages[2].regions:
        assert len(region.p_mw) == 15
        assert region.time_minutes[-1] == 14
        checks = {c.code: c for c in region.checks}
        assert checks["ROLLING_COMPLETE"].status == "unknown"
        assert (
            region.storage_energy_mwh[-1]
            == execution.rolling_updates[1].initial_energy_mwh[region.region]
        )
    assert not result.executive_summary.overall_passed


def test_future_minute_plant_changes_cannot_change_earlier_coordination():
    from oilfield_energy.bootstrap.adapters import cluster_rolling

    case = short_case()
    real_coordinate = cluster_rolling.run_admm_coordination
    outputs = []
    for factor in (1.0, 1.2):
        plants = engines._persistent_plant(case)
        plants = {
            name: plant.model_copy(
                update={
                    "load_p": tuple(
                        series.model_copy(
                            update={
                                "values": series.values[:30]
                                + tuple(v * factor for v in series.values[30:])
                            }
                        )
                        for series in plant.load_p
                    )
                }
            )
            for name, plant in plants.items()
        }
        count = 0

        def coordinate(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                raise RuntimeError("stop before the changed future is observed")
            return real_coordinate(*args, **kwargs)

        with (
            patch.object(
                engines,
                "_execution_inputs",
                return_value=(case, plants, "causal fixture"),
            ),
            patch.object(
                cluster_rolling, "run_admm_coordination", side_effect=coordinate
            ),
        ):
            outputs.append(
                run_simulation(
                    SimulationRequest(scenario_type="cluster_coordination", steps=4),
                    input_case=case,
                ).cluster_execution
            )
    for first, second in zip(
        outputs[0].rolling_updates[:2], outputs[1].rolling_updates[:2]
    ):
        assert first.initial_energy_mwh == second.initial_energy_mwh
        assert first.p_target_mw == second.p_target_mw
        assert first.q_target_mvar == second.q_target_mvar
    for first, second in zip(
        outputs[0].stages[2].regions, outputs[1].stages[2].regions
    ):
        assert len(first.p_mw) == 30
        assert first.p_mw == second.p_mw
        assert first.storage_energy_mwh == second.storage_energy_mwh

"""Synthetic radial bottlenecks: a feasible network must inform coordination.

No customer data. The independent branch bound and PCC conservation supplement
the production AC check; the centralized solve supplies a feasibility witness,
not the recovered device plan.
"""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.admm import CoordinationWorkspace, run_admm_coordination
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    ExecutionEngines,
    RegionalPlanRequest,
    diagnose_regional_reference,
    realize_regional_plan,
)
from oilfield_energy.bootstrap.adapters.cluster_recovery import (
    solve_with_reference_recovery,
)
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig
from oilfield_energy.modules.dispatch.api import build_coordination_calibration
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationRecoveryPolicy,
    NetworkCandidatePolicy,
    NetworkCoordinationPolicy,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    ExecutionPolicy,
)
from tests.support.network_case import bottleneck_case


def exact_calibration(case, *, storage=False):
    witness = solve_case_ac_consistent(
        case, storage_enabled=storage, time_limit_seconds=10
    )
    assert witness.passed, witness.stop_reason
    return {
        name: build_coordination_calibration(
            p_loss_mw=row["loss_mw"], q_loss_mvar=row["reactive_loss_mvar"]
        )
        for name, row in witness.optimization.microgrids.items()
    }


def recover(case, calibration, *, enabled=True, workspace=None, storage=False):
    policy = ExecutionPolicy(
        coordination_recovery=CoordinationRecoveryPolicy(),
        network_candidates=NetworkCandidatePolicy() if enabled else None,
    )
    config = ADMMConfig(max_iterations=1000)

    def coordinate(current, margin, candidates=()):
        return run_admm_coordination(
            case,
            storage_enabled=storage,
            loss_calibration=current,
            admm_config=replace(config, aggregate_pf_margin_mvar=margin),
            workspace=workspace,
            network_candidates={c.region: c for c in candidates}
            if candidates
            else None,
        )

    def request(window, name, reference):
        return RegionalPlanRequest(
            window,
            name,
            reference.p_references_mw[name],
            reference.q_references_mvar[name],
            storage,
            10,
            policy,
        )

    return solve_with_reference_recovery(
        case=case,
        policy=policy,
        calibration=calibration,
        coordinate=coordinate,
        coordinate_candidates=coordinate,
        aggregate_pf_target=0.95,
        solve_regions=lambda w, r: {
            m.name: realize_regional_plan(request(w, m.name, r)) for m in w.microgrids
        },
        diagnose_regions=lambda w, r, n: tuple(
            diagnose_regional_reference((request(w, m.name, r), n))
            for m in w.microgrids
        ),
    )


@pytest.mark.parametrize(
    "rating,voltage_max,loss_weight", [(1.0, 1.05, 650.0), (5.0, 1.001, 650000.0)]
)
def test_network_witness_changes_coordination_even_when_losses_are_already_exact(
    rating, voltage_max, loss_weight
):
    case = bottleneck_case(rating=rating, voltage_max=voltage_max)
    # The upper-voltage relaxation needs a strong loss objective to obtain an
    # AC-certified witness. Default-weight recovery uses a certified diagnostic
    # instead and is tested separately below.
    case = replace(
        case, assumptions=replace(case.assumptions, loss_value_cny_per_mwh=loss_weight)
    )
    calibration = exact_calibration(case)
    old = recover(case, calibration, enabled=False)
    assert not old.report.accepted and old.report.stop_reason == "stagnated"
    outcome = recover(case, calibration)
    assert outcome.report.accepted, outcome.report
    assert len(outcome.report.rounds) == 2
    first, final = outcome.report.rounds
    assert first.candidate_selection.status == "selected"
    assert final.network_candidates_used == first.candidate_selection.candidates
    assert final.admm_iterations > 0 and outcome.payload.reference.converged
    assert outcome.payload.reference.coordination_snapshot is not None
    for name, (row, plan) in outcome.payload.plans.items():
        assert row.executable_plan
        assert row.p_mw[0] > 4.0
        physical = plan.microgrids[name]
        np.testing.assert_allclose(
            row.p_mw, 5.0 - physical["pv_used_mw"] + physical["loss_mw"], atol=1e-6
        )
        if rating == 1.0:
            assert max(physical["pv_used_mw"]) <= 1.0 + 1e-6
        assert final.calibration_used[name] == next(
            c.calibration for c in final.network_candidates_used if c.region == name
        )
    assert (
        CoordinationRecoveryReport.model_validate_json(outcome.report.model_dump_json())
        == outcome.report
    )
    assert all(np.array_equal(m.pv_available_mw["pv"], [4.0]) for m in case.microgrids)


def test_relaxed_line_needs_no_candidate_and_cache_drops_old_candidate_constraints():
    workspace = CoordinationWorkspace()
    narrow = bottleneck_case()
    calibration = exact_calibration(narrow)
    assert recover(narrow, calibration, workspace=workspace).report.accepted
    # Same topology and parameters: ordinary solve must remove the last anchor.
    ordinary = run_admm_coordination(
        narrow, storage_enabled=False, loss_calibration=calibration, workspace=workspace
    )
    assert ordinary.converged and ordinary.p_references_mw["A"][0] < 2.0
    wide = bottleneck_case(rating=5.0)
    result = recover(wide, exact_calibration(wide), workspace=workspace)
    assert result.report.accepted and len(result.report.rounds) == 1
    assert result.report.rounds[0].candidate_selection is None


def test_day_ahead_uses_network_feedback_before_adoption():
    case = bottleneck_case()
    calibration = exact_calibration(case)
    config = ADMMConfig(max_iterations=1000)
    reference = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration, admm_config=config
    )
    policy = ExecutionPolicy(
        coordination_recovery=CoordinationRecoveryPolicy(),
        network_candidates=NetworkCandidatePolicy(),
    )
    engines = ExecutionEngines(
        case,
        case,
        {},
        reference,
        False,
        10,
        policy,
        config,
        loss_calibration=calibration,
    )
    rows = engines.run_stage("day_ahead")
    assert engines.day_ahead_recovery.accepted, engines.day_ahead_recovery
    assert all(r.executable_plan and r.p_mw[0] > 4 for r in rows)


def test_voltage_bottleneck_recovers_from_certified_diagnostic_with_default_weights():
    case = bottleneck_case(rating=5.0, voltage_max=1.001)
    zero_pv = replace(
        case,
        microgrids=[
            replace(m, pv_available_mw={"pv": np.zeros(1)}) for m in case.microgrids
        ],
    )
    result = recover(case, exact_calibration(zero_pv))
    assert result.report.accepted, result.report
    assert result.report.rounds[-1].network_candidates_used
    for feedback in result.report.rounds[0].feedback:
        assert feedback.evidence.status == "passed"
        assert all(c.status == "passed" for c in feedback.evidence.checks)
    assert all(row.executable_plan for row, _ in result.payload.plans.values())


def test_multi_period_storage_keeps_whole_energy_trajectory():
    case = bottleneck_case(steps=2)
    case = replace(case, price_cny_per_mwh=np.array([800.0, 200.0]))
    result = recover(case, exact_calibration(case, storage=True), storage=True)
    assert result.report.accepted, result.report
    assert result.report.rounds[-1].network_candidates_used
    for m in case.microgrids:
        row, plan = result.payload.plans[m.name]
        assert row.executable_plan
        values = plan.microgrids[m.name]
        np.testing.assert_allclose(
            np.diff(row.storage_energy_mwh),
            case.assumptions.dt_hours
            * (
                m.storage.eta_charge * values["storage_charge_mw"]
                - values["storage_discharge_mw"] / m.storage.eta_discharge
            ),
            atol=1e-6,
        )


@pytest.mark.parametrize("network_frontend", [False, True])
@pytest.mark.parametrize("minute_replan", [False, True])
def test_rolling_and_short_window_use_network_information_without_advancing_state(
    monkeypatch, minute_replan, network_frontend
):
    from oilfield_energy.bootstrap.adapters import cluster_rolling

    case = bottleneck_case(steps=2)
    calibration = exact_calibration(case)
    config = ADMMConfig(max_iterations=1000)
    reference = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration, admm_config=config
    )
    policy = ExecutionPolicy(
        coordination_recovery=CoordinationRecoveryPolicy(),
        network_candidates=NetworkCandidatePolicy(),
        network_coordination=NetworkCoordinationPolicy() if network_frontend else None,
    )
    engines = ExecutionEngines(
        case,
        case,
        {},
        reference,
        False,
        10,
        policy,
        config,
        loss_calibration=calibration,
    )
    ahead = engines.run_stage("day_ahead")
    assert engines.day_ahead_recovery.accepted

    def strict(window, ref):
        return {
            m.name: realize_regional_plan(engines.plan_request(window, m.name, ref))
            for m in window.microgrids
        }

    def diagnostics(window, ref, number):
        return tuple(
            diagnose_regional_reference(
                (engines.plan_request(window, m.name, ref), number)
            )
            for m in window.microgrids
        )

    from tests.support.cluster_planning_port import install_planning_port

    policy = install_planning_port(monkeypatch, policy)
    engine = cluster_rolling.RollingClusterEngine(
        case=case,
        original_case=case,
        plants={m.name: None for m in case.microgrids},
        original_admm=engines.admm,
        day_ahead=ahead,
        storage_enabled=False,
        policy=policy,
        admm_config=config,
        communication_config=CommunicationConfig(),
        loss_calibration=calibration,
        solve_regions=strict,
        diagnose_regions=diagnostics,
        progress=lambda _: None,
    )

    engine.current_terminal = dict(engine.energies)
    initial = dict(engine.energies)
    update = (
        engine.prepare(7, 15, 15, minute_replan=True)
        if minute_replan
        else engine.prepare(0, 15, 30)
    )
    assert update.adopted, update.reason
    if network_frontend:
        assert (
            update.reference_recovery.rounds[-1].reference_method == "network-socp-admm"
        )
    else:
        assert update.reference_recovery.rounds[-1].network_candidates_used
    assert engine.energies == initial
    assert all(s.minute == 0 for s in engine.checkpoints.values())
    expected = tuple(map(float, range(7, 15))) if minute_replan else (0.0, 15.0)
    assert all(
        r.time_minutes == expected for r in update.reference_recovery.rounds[-1].regions
    )
    assert all(
        c.time_minutes == expected
        for c in update.reference_recovery.rounds[-1].network_candidates_used
    )

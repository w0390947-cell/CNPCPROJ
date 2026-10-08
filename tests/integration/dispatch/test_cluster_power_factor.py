"""F3: real ADMM + regional MISOCP/AC, without fixed shunt compensation."""

from dataclasses import replace
from math import fsum, hypot

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.admm import CoordinationWorkspace, run_admm_coordination
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    ExecutionEngines,
    RegionalPlanRequest,
    realize_regional_plan,
)
from oilfield_energy.bootstrap.adapters.cluster_recovery import (
    solve_with_reference_recovery,
)
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.modules.dispatch.api import build_coordination_calibration
from oilfield_energy.modules.dispatch.contracts import CoordinationRecoveryPolicy
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    ExecutionPolicy,
)


def physical_case(support=0.805):
    base = build_synthetic_case(4)
    regions = []
    for old in base.microgrids:
        load = {b: v[:1].copy() for b, v in old.load_p_mw.items()}
        network = replace(
            old.network_model_v2,
            shunts=(),
            operating_modes=tuple(
                replace(mode, shunt_operating_points=())
                for mode in old.network_model_v2.operating_modes
            ),
        )
        regions.append(
            replace(
                old,
                load_p_mw=load,
                load_q_mvar={b: 0.37 * v for b, v in load.items()},
                network_model_v2=network,
                wind_available_mw={b: np.zeros(1) for b in old.wind_available_mw},
                pv_available_mw={b: np.zeros(1) for b in old.pv_available_mw},
                wind_q_abs_max_mvar={b: 0.0 for b in old.wind_capacity_mw},
                pv_reactive_enabled={b: False for b in old.pv_available_mw},
                storage_reactive_enabled=False,
                svg_q_min_mvar=0,
                svg_q_max_mvar=0 if old.name == "SC" else support,
            )
        )
    return replace(
        base,
        microgrids=regions,
        time_hours=np.zeros(1),
        price_cny_per_mwh=base.price_cny_per_mwh[:1],
    )


def setup_problem(support=0.805, *, budget=3):
    case = physical_case(support)
    physical = solve_case_ac_consistent(
        case, storage_enabled=False, time_limit_seconds=20
    )
    assert physical.passed
    # Explicit calibration offsets smaller than local tracking hard limits.
    # They model bounded reference/realization mismatch, without the F1 omission.
    calibration = {
        n: build_coordination_calibration(
            p_loss_mw=r["loss_mw"] - 0.002,
            q_loss_mvar=r["reactive_loss_mvar"] - (0.03 if n == "SC" else 0),
        )
        for n, r in physical.optimization.microgrids.items()
    }
    config = ADMMConfig(max_iterations=400, local_q_regularization=0)
    policy = ExecutionPolicy(
        coordination_recovery=CoordinationRecoveryPolicy(max_recoordinations=budget)
    )
    reference = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration, admm_config=config
    )
    assert reference.converged
    return case, calibration, config, policy, reference


@pytest.mark.parametrize(
    "stage,reuse", [("day_ahead", False), ("intraday", False), ("intraday", True)]
)
def test_joint_pf_failure_recovers_through_real_reference_and_device_resolves(
    stage, reuse
):
    case, calibration, config, policy, reference = setup_problem()
    saved = dict(calibration)
    if stage == "day_ahead":
        engine = ExecutionEngines(
            case,
            case,
            {},
            reference,
            False,
            20,
            policy,
            admm_config=config,
            loss_calibration=calibration,
        )
        final = engine.run_stage("day_ahead")
        report = engine.day_ahead_recovery
    else:
        workspace = CoordinationWorkspace() if reuse else None

        def coordinate(current, margin):
            return run_admm_coordination(
                case,
                storage_enabled=False,
                loss_calibration=current,
                workspace=workspace,
                admm_config=replace(config, aggregate_pf_margin_mvar=margin),
            )

        def solve(window, ref):
            return {
                m.name: realize_regional_plan(
                    RegionalPlanRequest(
                        window,
                        m.name,
                        ref.p_references_mw[m.name],
                        ref.q_references_mvar[m.name],
                        False,
                        20,
                        policy,
                    )
                )
                for m in window.microgrids
            }

        # PF recovery uses strict physical plans, not soft calibration diagnostics.
        recovered = solve_with_reference_recovery(
            case=case,
            policy=policy,
            calibration=calibration,
            coordinate=coordinate,
            aggregate_pf_target=config.aggregate_pf_target,
            solve_regions=solve,
            diagnose_regions=None,
        )
        report = recovered.report
        final = tuple(r for r, _ in recovered.payload.plans.values())
    assert report.accepted, report
    assert len(report.rounds) == 3
    first = report.rounds[0]
    assert first.outcome == "power_factor_rejected"
    assert all(r.executable_plan for r in first.regions)
    assert first.cluster_import.status == "passed"
    pr = fsum(v[0] for v in first.p_target_mw.values())
    qr = fsum(v[0] for v in first.q_target_mvar.values())
    assert pr / hypot(pr, qr) >= 0.95 - 1e-8
    assert first.cluster_power_factor.points[0].power_factor == pytest.approx(
        0.9498458321, abs=2e-7
    )
    assert report.rounds[-1].cluster_power_factor.points[0].power_factor >= 0.95
    assert all(r.executable_plan for r in final)
    assert not any(r.feedback for r in report.rounds)
    assert all(r.calibration_used == saved for r in report.rounds)
    assert calibration == saved
    # Final physical plans retain the original regional hard tracking limits.
    for r in final:
        assert max(abs(a - b) for a, b in zip(r.q_mvar, r.q_target_mvar)) <= 0.050001
    assert (
        CoordinationRecoveryReport.model_validate_json(report.model_dump_json())
        == report
    )


def test_exhausted_pf_budget_prevents_day_ahead_adoption():
    case, calibration, config, policy, ref = setup_problem(budget=1)
    engine = ExecutionEngines(
        case,
        case,
        {},
        ref,
        False,
        20,
        policy,
        admm_config=config,
        loss_calibration=calibration,
    )
    rows = engine.run_stage("day_ahead")
    report = engine.day_ahead_recovery
    assert not report.accepted and report.stop_reason == "budget_exhausted"
    assert len(report.rounds) == 2
    assert all(not r.executable_plan for r in rows)
    assert all(r.economic_cost_cny is None for r in rows)
    assert report.rounds[-1].cluster_power_factor.status == "violated"


def test_nonbinding_pf_retains_first_plan_and_custom_target_is_used():
    case, calibration, config, policy, _ = setup_problem()
    config = replace(config, aggregate_pf_target=0.94)
    ref = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration, admm_config=config
    )
    engine = ExecutionEngines(
        case,
        case,
        {},
        ref,
        False,
        20,
        policy,
        admm_config=config,
        loss_calibration=calibration,
    )
    rows = engine.run_stage("day_ahead")
    report = engine.day_ahead_recovery
    assert report.accepted and len(report.rounds) == 1
    assert report.power_factor_policy.target == 0.94
    assert engine.admm is ref
    assert all(r.executable_plan for r in rows)


@pytest.mark.parametrize("minute_replan", [False, True])
def test_actual_rolling_entry_recovers_whole_window_before_plant_advances(
    monkeypatch, minute_replan
):
    from oilfield_energy.bootstrap.adapters import cluster_rolling
    from oilfield_energy.hierarchy_types import CommunicationConfig

    case, calibration, config, policy, reference = setup_problem()
    engines = ExecutionEngines(
        case,
        case,
        {},
        reference,
        False,
        20,
        policy,
        admm_config=config,
        loss_calibration=calibration,
    )
    day = engines.run_stage("day_ahead")

    def solve(window, ref):
        return {
            m.name: realize_regional_plan(engines.plan_request(window, m.name, ref))
            for m in window.microgrids
        }

    from tests.support.cluster_planning_port import install_planning_port

    policy = install_planning_port(monkeypatch, policy)
    rolling = cluster_rolling.RollingClusterEngine(
        case=case,
        original_case=case,
        plants={m.name: None for m in case.microgrids},
        original_admm=engines.admm,
        day_ahead=day,
        storage_enabled=False,
        policy=policy,
        admm_config=config,
        communication_config=CommunicationConfig(),
        loss_calibration=calibration,
        solve_regions=solve,
        diagnose_regions=None,
        progress=lambda _: None,
    )

    rolling.current_terminal = dict(rolling.energies)
    initial = dict(rolling.energies)
    update = (
        rolling.prepare(7, 15, 15, minute_replan=True)
        if minute_replan
        else rolling.prepare(0, 15, 15)
    )
    assert update.adopted, update.reason
    report = update.reference_recovery
    assert report.accepted and len(report.rounds) > 1
    assert report.rounds[0].cluster_power_factor.status == "violated"
    expected = tuple(map(float, range(7, 15))) if minute_replan else (0.0,)
    assert (
        tuple(p.time_minutes for p in report.rounds[-1].cluster_power_factor.points)
        == expected
    )
    assert all(
        p.status == "passed" for p in report.rounds[-1].cluster_power_factor.points
    )
    assert rolling.energies == initial and len(rolling.safety.candidates) == 1
    assert all(c.minute == 0 for c in rolling.checkpoints.values())

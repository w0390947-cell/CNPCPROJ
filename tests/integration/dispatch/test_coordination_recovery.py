"""F4: current regional physics must repair stale references without a central oracle."""

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
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig
from oilfield_energy.modules.dispatch.api import build_coordination_calibration
from oilfield_energy.modules.dispatch.contracts import CoordinationRecoveryPolicy
from oilfield_energy.network_model import (
    NetworkOperatingMode,
    ShuntCompensator,
    ShuntKind,
    ShuntOperatingPoint,
)
from oilfield_energy.workflows.cluster_execution.api import executable_regional_plan
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    ExecutionPolicy,
)


def fixed_load_case(scale=1.0, *, shunt=False, steps=1):
    base = build_synthetic_case(4)
    regions = []
    for mg in base.microgrids:
        load = {b: np.full(steps, v[0] * scale) for b, v in mg.load_p_mw.items()}
        network = mg.network_model_v2
        mode_id = mg.network_operating_mode_id
        if shunt and mg.name == "SC":
            capacitor = ShuntCompensator(
                "F4_SHUNT", mg.svg_bus, ShuntKind.CAPACITOR, 0.1, 0, 4, 0
            )
            mode = NetworkOperatingMode(
                "f4-fixed",
                "Synthetic fixed compensation",
                shunt_operating_points=(
                    ShuntOperatingPoint(capacitor.shunt_id, True, 2),
                ),
            )
            network = replace(
                network,
                shunts=(capacitor,),
                operating_modes=(mode,),
                default_operating_mode_id=mode.mode_id,
            )
            mode_id = mode.mode_id
        regions.append(
            replace(
                mg,
                load_p_mw=load,
                load_q_mvar={b: 0.1 * v for b, v in load.items()},
                wind_available_mw={b: np.zeros(steps) for b in mg.wind_available_mw},
                pv_available_mw={b: np.zeros(steps) for b in mg.pv_available_mw},
                wind_q_abs_max_mvar={b: 0 for b in mg.wind_capacity_mw},
                pv_reactive_enabled={b: False for b in mg.pv_available_mw},
                storage_reactive_enabled=False,
                svg_q_min_mvar=0,
                svg_q_max_mvar=0,
                network_model_v2=network,
                network_operating_mode_id=mode_id,
            )
        )
    return replace(
        base,
        microgrids=regions,
        time_hours=np.arange(steps) / 4,
        price_cny_per_mwh=base.price_cny_per_mwh[:steps],
        assumptions=replace(base.assumptions, dt_hours=0.25),
    )


def old_calibration(case):
    checked = solve_case_ac_consistent(
        case, storage_enabled=False, time_limit_seconds=20
    )
    assert checked.passed
    return {
        name: build_coordination_calibration(
            p_loss_mw=row["loss_mw"],
            q_loss_mvar=row["reactive_loss_mvar"],
            shunt_nominal_mvar_by_bus=row["fixed_shunt_q_nominal_mvar_by_bus"],
            shunt_q_mvar_by_bus=row["fixed_shunt_q_mvar_by_bus"],
        )
        for name, row in checked.optimization.microgrids.items()
    }


def solve_problem(case, calibration, *, policy=None, workspace=None, diagnose=None):
    policy = policy or ExecutionPolicy(
        coordination_recovery=CoordinationRecoveryPolicy()
    )

    def request(window, name, reference):
        return RegionalPlanRequest(
            window,
            name,
            reference.p_references_mw[name],
            reference.q_references_mvar[name],
            False,
            20,
            policy,
        )

    def diagnostics(window, reference, number):
        return tuple(
            diagnose_regional_reference((request(window, m.name, reference), number))
            for m in window.microgrids
        )

    return solve_with_reference_recovery(
        case=case,
        policy=policy,
        calibration=calibration,
        aggregate_pf_target=0.95,
        coordinate=lambda current, margin: run_admm_coordination(
            case,
            storage_enabled=False,
            loss_calibration=current,
            admm_config=ADMMConfig(max_iterations=400, aggregate_pf_margin_mvar=margin),
            workspace=workspace,
        ),
        solve_regions=lambda window, reference: {
            m.name: realize_regional_plan(request(window, m.name, reference))
            for m in window.microgrids
        },
        diagnose_regions=diagnose or diagnostics,
    )


@pytest.mark.parametrize("shunt,reuse", [(False, False), (False, True), (True, True)])
def test_stale_losses_recovered_from_current_regional_diagnostics(shunt, reuse):
    calibration = old_calibration(fixed_load_case(0.85, shunt=shunt))
    saved = dict(calibration)
    case = fixed_load_case(shunt=shunt)
    saved_load = {
        m.name: {b: v.copy() for b, v in m.load_p_mw.items()} for m in case.microgrids
    }
    # No centralized current-forecast solve is supplied to recovery.
    outcome = solve_problem(
        case, calibration, workspace=CoordinationWorkspace() if reuse else None
    )
    assert outcome.report.accepted, outcome.report
    assert outcome.payload is not None
    rounds = outcome.report.rounds
    assert len(rounds) == 2
    assert rounds[0].outcome == "regional_rejected"
    rejected = {
        r.region
        for r in rounds[0].regions
        if r.solver_stop_reason == "solver_infeasible"
    }
    assert rejected == {"YA_B", "YA_C"}
    assert all(f.evidence.status == "passed" for f in rounds[0].feedback)
    assert all(not executable_regional_plan(f.evidence) for f in rounds[0].feedback)
    assert rounds[1].calibration_used == {
        f.evidence.region: f.calibration for f in rounds[0].feedback
    }
    assert any(
        rounds[0].q_target_mvar[n] != rounds[1].q_target_mvar[n] for n in rejected
    )
    for mg in case.microgrids:
        regional, plan = outcome.payload.plans[mg.name]
        assert plan is not None and executable_regional_plan(regional)
        # Independent fixed-resource bus conservation at the final physical point.
        row = plan.microgrids[mg.name]
        expected_p = sum(mg.load_p_mw.values()) + row["loss_mw"]
        expected_q = (
            sum(mg.load_q_mvar.values())
            + row["reactive_loss_mvar"]
            - sum(row["fixed_shunt_q_mvar_by_bus"].values())
        )
        np.testing.assert_allclose(regional.p_mw, expected_p, atol=1e-6, rtol=0)
        np.testing.assert_allclose(regional.q_mvar, expected_q, atol=1e-6, rtol=0)
        assert max(abs(np.array(regional.p_mw) - regional.p_target_mw)) <= 0.050001
        assert max(abs(np.array(regional.q_mvar) - regional.q_target_mvar)) <= 0.050001
        for bus, load in mg.load_p_mw.items():
            np.testing.assert_array_equal(load, saved_load[mg.name][bus])
    assert calibration == saved
    assert (
        CoordinationRecoveryReport.model_validate_json(outcome.report.model_dump_json())
        == outcome.report
    )
    if shunt:
        new_shunt = rounds[1].calibration_used["SC"].fixed_shunts[0]
        assert new_shunt.nominal_q_mvar == 0.2
        assert new_shunt.q_mvar != calibration["SC"].fixed_shunts[0].q_mvar
    print(
        "recovery",
        shunt,
        reuse,
        {n: (rounds[0].q_target_mvar[n], rounds[1].q_target_mvar[n]) for n in rejected},
    )


def test_first_pass_does_not_invoke_diagnostics():
    case = fixed_load_case()

    def forbidden(*_):
        pytest.fail("a valid first plan needs no recovery")

    outcome = solve_problem(case, old_calibration(case), diagnose=forbidden)
    assert outcome.report.accepted and len(outcome.report.rounds) == 1
    assert not outcome.report.rounds[0].feedback


def test_successful_local_plans_still_require_joint_capacity():
    case = fixed_load_case()
    calibration = old_calibration(case)
    reference = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration
    )
    limit = float(sum(reference.p_references_mw.values())[0]) - 0.2
    case = replace(case, cluster_import_limit_mw=limit)
    policy = ExecutionPolicy(coordination_recovery=CoordinationRecoveryPolicy())
    engines = ExecutionEngines(
        case, case, {}, reference, False, 20, policy, loss_calibration=calibration
    )
    rows = engines.run_stage("day_ahead")
    report = engines.day_ahead_recovery
    assert not report.accepted and report.stop_reason == "joint_rejected"
    assert report.rounds[0].cluster_import.status == "violated"
    assert all(executable_regional_plan(r) for r in report.rounds[0].regions)
    assert all(not executable_regional_plan(r) for r in rows)
    assert not report.rounds[0].feedback


def test_parallel_regional_diagnostics_round_trip_typed_witnesses():
    from oilfield_energy.runtime.parallel import OwnedProcessPool

    case = fixed_load_case()
    calibration = old_calibration(fixed_load_case(0.85))
    reference = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration
    )
    engines = ExecutionEngines(case, case, {}, reference, False, 20, ExecutionPolicy())
    with OwnedProcessPool(2) as pool:
        engines.plan_pool = pool
        feedback = engines.diagnose_windows(case, reference, 3)
        engines.plan_pool = None
    assert {f.evidence.region for f in feedback} == {m.name for m in case.microgrids}
    assert all(f.reference_round == 3 and f.calibration is not None for f in feedback)
    assert all(not executable_regional_plan(f.evidence) for f in feedback)


def test_disabled_recovery_preserves_rejection():
    outcome = solve_problem(
        fixed_load_case(),
        old_calibration(fixed_load_case(0.85)),
        policy=ExecutionPolicy(),
    )
    assert not outcome.report.accepted
    assert outcome.report.stop_reason == "disabled"
    assert len(outcome.report.rounds) == 1


def test_diagnostic_cannot_bypass_physical_voltage_constraints():
    case = fixed_load_case()
    # SC has positive resistive drops and no adjustable Q; every bus cannot be
    # held above 1.0 pu with the slack at 1.0 pu. The reference is merely soft.
    case = replace(
        case, microgrids=[replace(m, voltage_min_pu=1.0) for m in case.microgrids]
    )
    request = RegionalPlanRequest(
        case, "SC", np.array([0.0]), np.array([0.0]), False, 20, ExecutionPolicy()
    )
    feedback = diagnose_regional_reference((request, 1))
    assert feedback.calibration is None
    assert feedback.evidence.status != "passed"
    assert feedback.evidence.solver_stop_reason == "solver_infeasible"


@pytest.mark.parametrize("failure", ["ac", "quality", "timeout"])
def test_untrusted_numerical_diagnostic_never_returns_calibration(monkeypatch, failure):
    from oilfield_energy.bootstrap.adapters import cluster_execution
    from oilfield_energy.modules.dispatch.contracts import (
        OptimizationAttempt,
        OptimizationQuality,
    )

    case = fixed_load_case()
    checked = solve_case_ac_consistent(
        case, ["SC"], storage_enabled=False, time_limit_seconds=20
    )
    assert checked.passed
    if failure == "ac":
        checked = replace(
            checked,
            passed=False,
            stop_reason="ac_consistency_iterations_exhausted",
            ac_validation={
                "passed": False,
                "microgrids": {"SC": {"all_converged": False}},
            },
        )
    elif failure == "timeout":
        checked = replace(
            checked,
            passed=False,
            stop_reason="solver_time_limit_without_plan",
            optimization=replace(
                checked.optimization, success=False, message="SCIP status: timelimit"
            ),
        )
    else:
        checked = replace(
            checked,
            optimization_quality=(
                OptimizationQuality(
                    status="budget_exhausted",
                    target_relative_gap=1e-4,
                    selected_attempt=1,
                    attempts=(
                        OptimizationAttempt(
                            budget_seconds=20,
                            solver_status="timelimit",
                            feasible=True,
                            objective_cny=1,
                            relative_gap=0.1,
                        ),
                    ),
                ),
            ),
        )
    monkeypatch.setattr(
        cluster_execution, "solve_case_ac_consistent", lambda *a, **kw: checked
    )
    feedback = diagnose_regional_reference(
        (
            RegionalPlanRequest(
                case,
                "SC",
                np.array([0.0]),
                np.array([0.0]),
                False,
                20,
                ExecutionPolicy(),
            ),
            1,
        )
    )
    assert feedback.calibration is None
    assert feedback.evidence.status == "unknown"
    assert not executable_regional_plan(feedback.evidence)


def test_diagnostic_preserves_storage_terminal_reserve_and_reactive_envelopes():
    from oilfield_energy.bootstrap.adapters.cluster_reactive import (
        with_reactive_planning,
    )
    from oilfield_energy.bootstrap.adapters.cluster_reserve import with_storage_reserves
    from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy
    from oilfield_energy.modules.dispatch.contracts import (
        ReactivePlanningPolicy,
        StorageReservePolicy,
    )

    case = build_synthetic_case(4)
    case = replace(
        case,
        time_hours=np.arange(4) / 4,
        assumptions=replace(case.assumptions, dt_hours=0.25),
    )
    case = with_storage_reserves(case, StorageReservePolicy(), 60)
    case = with_reactive_planning(
        case,
        policy=ReactivePlanningPolicy(),
        dynamics=DynamicTrackingPolicy(),
        checkpoints={},
        storage_enabled=True,
    )
    mg = case.microgrids[0]
    feedback = diagnose_regional_reference(
        (
            RegionalPlanRequest(
                case,
                mg.name,
                np.full(4, -100.0),
                np.full(4, -100.0),
                True,
                20,
                ExecutionPolicy(),
            ),
            1,
        )
    )
    assert feedback.calibration is not None, feedback.evidence
    row = feedback.evidence
    checks = {c.code: c.status for c in row.checks}
    assert all(
        checks[key] == "passed"
        for key in (
            "DEVICE_PLAN",
            "AC_NETWORK",
            "STORAGE_RESERVE_CAPACITY",
            "STORAGE_RESERVE_POWER",
            "STORAGE_RESERVE_ENERGY",
            "REACTIVE_PLAN_ENVELOPE",
        )
    )
    assert row.storage_energy_mwh[0] == pytest.approx(
        mg.storage.e_initial_mwh, abs=1e-6
    )
    assert (
        abs(row.storage_energy_mwh[-1] - mg.storage.terminal_energy_mwh)
        <= case.assumptions.terminal_energy_tolerance_mwh + 1e-6
    )
    assert not executable_regional_plan(row)


@pytest.mark.parametrize("workers", [1, 2])
def test_day_ahead_entry_uses_same_recovery_and_records_final_reference(workers):
    case = fixed_load_case()
    calibration = old_calibration(fixed_load_case(0.85))
    reference = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration
    )
    policy = ExecutionPolicy(
        regional_plan_workers=workers,
        coordination_recovery=CoordinationRecoveryPolicy(),
    )
    engines = ExecutionEngines(
        case, case, {}, reference, False, 20, policy, loss_calibration=calibration
    )
    rows = engines.run_stage("day_ahead")
    assert all(executable_regional_plan(r) for r in rows)
    assert engines.day_ahead_recovery.accepted
    assert len(engines.day_ahead_recovery.rounds) == 2
    assert engines.admm is not reference
    for row in rows:
        np.testing.assert_array_equal(
            row.q_target_mvar, engines.admm.q_references_mvar[row.region]
        )
    assert engines.plan_pool is None


@pytest.mark.parametrize("minute_replan", [False, True])
def test_rolling_prepare_recovers_before_adoption_without_advancing_state(
    monkeypatch, minute_replan
):
    from oilfield_energy.bootstrap.adapters import cluster_rolling

    case = fixed_load_case(steps=2)
    calibration = old_calibration(fixed_load_case(0.85, steps=2))
    reference = run_admm_coordination(
        case, storage_enabled=False, loss_calibration=calibration
    )
    policy = ExecutionPolicy(coordination_recovery=CoordinationRecoveryPolicy())
    engines = ExecutionEngines(
        case, case, {}, reference, False, 20, policy, loss_calibration=calibration
    )
    day_ahead = engines.run_stage("day_ahead")

    def strict(window, ref):
        return {
            m.name: realize_regional_plan(engines.plan_request(window, m.name, ref))
            for m in window.microgrids
        }

    def diagnostic(window, ref, number):
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
        day_ahead=day_ahead,
        storage_enabled=False,
        policy=policy,
        admm_config=ADMMConfig(),
        communication_config=CommunicationConfig(),
        loss_calibration=calibration,
        solve_regions=strict,
        diagnose_regions=diagnostic,
        progress=lambda _: None,
    )

    # Only the device session boundary is replaced; all planning, feedback,
    # coordinator, regional optimization and independent AC checks are real.

    engine.current_terminal = dict(engine.energies)
    initial = dict(engine.energies)
    update = (
        engine.prepare(7, 15, 15, minute_replan=True)
        if minute_replan
        else engine.prepare(0, 15, 30)
    )
    assert update.adopted, update.reason
    assert update.reference_recovery.accepted
    assert len(update.reference_recovery.rounds) == 2
    assert engine.energies == initial
    assert all(checkpoint.minute == 0 for checkpoint in engine.checkpoints.values())
    assert len(engine.safety.candidates) == 1
    expected = tuple(
        (np.arange(7, 15) if minute_replan else np.array([0, 15])).astype(float)
    )
    assert all(
        f.evidence.time_minutes == expected
        for f in update.reference_recovery.rounds[0].feedback
    )

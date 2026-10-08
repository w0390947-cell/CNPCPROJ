"""Actual SCIP and independent AC checks; no feasibility threshold is relaxed."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy import ac_consistency
from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.ac_power_flow import validate_ac_dispatch
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkOperatingMode,
    ShuntCompensator,
    ShuntKind,
    ShuntOperatingPoint,
)
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    RegionalPlanRequest,
    realize_regional_plan,
)
from oilfield_energy.modules.dispatch.contracts import (
    ACRecoveryPolicy,
    PCCTrackingLimits,
    JointNetworkTarget,
)
from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy
from oilfield_energy.modules.dispatch.api import plan_storage_reserve
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    RegionalExecution,
)
from oilfield_energy.workflows.cluster_execution.api import verify_execution
from tests.support.ac_recovery import ac_tracking_case, targets

LIMITS = PCCTrackingLimits(0.05, 0.05, 1e-6)


def solve(case, **changes):
    return solve_case_ac_consistent(
        case,
        cluster_coordination=False,
        pcc_targets=targets(case),
        tracking_limits=LIMITS,
        time_limit_seconds=10,
        **changes,
    )


def test_original_counterexample_recovers_under_original_weights_and_hard_limits():
    case = ac_tracking_case()
    old = solve(case, recovery_policy=None)
    assert not old.passed and old.stop_reason == "ac_consistency_iterations_exhausted"
    result = solve(case)
    assert result.passed and result.recovery.outcome == "certified"
    assert result.recovery.original_optimality == "proven"
    assert result.recovery.attempts[0].objective == "constant_feasibility"
    assert not result.recovery.attempts[0].original_optimality_proven
    report = result.ac_validation["microgrids"]["A"]
    assert abs(report["pcc_p_mw"][0] - 7.05) <= 0.05 + 1e-6
    assert abs(report["pcc_q_mvar"][0] - 0.05) <= 0.05 + 1e-6
    assert result.optimization.cluster["network_physics"] == "branch-flow-equality"
    assert result.optimization.cluster["original_constraints_verified"]
    assert result.recovery.attempts[-1].checks["A:q_tracking"]
    np.testing.assert_array_equal(case.microgrids[0].load_p_mw["load"], [7.0])


def fail_refinement(monkeypatch):
    actual = ac_consistency.solve_case_misocp

    def backend(*args, **kwargs):
        if (
            kwargs.get("exact_ac_physics")
            and kwargs.get("objective_mode") == "original"
        ):
            raise RuntimeError("injected refinement backend failure")
        return actual(*args, **kwargs)

    monkeypatch.setattr(ac_consistency, "solve_case_misocp", backend)


@pytest.mark.parametrize("allowed", [True, False])
def test_regional_adoption_preserves_explicit_unproven_economics(monkeypatch, allowed):
    fail_refinement(monkeypatch)
    case = ac_tracking_case()
    e, plan = realize_regional_plan(
        RegionalPlanRequest(
            case,
            "A",
            np.array([7.05]),
            np.array([0.05]),
            True,
            10,
            ExecutionPolicy(
                ac_recovery=ACRecoveryPolicy(allow_feasible_adoption=allowed)
            ),
        )
    )
    assert (plan is not None) == allowed
    assert e.executable_plan == allowed
    assert e.ac_recovery.selected_attempt == 1
    assert e.ac_recovery.original_optimality == "unproven"
    check = next(c for c in e.checks if c.code == "OPTIMIZATION_QUALITY")
    assert check.status == "unknown" and check.actual is None
    assert RegionalExecution.model_validate_json(e.model_dump_json()) == e
    if allowed:
        assert (
            plan.objective_cny > 0
        )  # Never export the zero feasibility objective as economic cost.
        blocked = e.model_copy(
            update={
                "checks": e.checks
                + (
                    e.checks[0].model_copy(
                        update={"code": "STORAGE_RESERVE_POWER", "status": "unknown"}
                    ),
                )
            }
        )
        assert not blocked.executable_plan
        summary = verify_execution(
            names=("A",),
            limit_mw=30.0,
            converged=True,
            storage_enabled=True,
            policy=ExecutionPolicy(),
            input_basis="synthetic plan evidence only",
            dataset_sha256=None,
            run_stage=lambda stage: (e,) if stage == "day_ahead" else (),
            progress=lambda _: None,
        )
        assert summary.stages[0].status == "passed"
        assert summary.stages[0].cluster_import.status == "passed"


def test_independent_ac_tracking_limit_is_not_replaced_by_optimizer_slacks(monkeypatch):
    original = ac_consistency.validate_ac_dispatch

    def drift(*args, **kwargs):
        report = original(*args, **kwargs)
        report["microgrids"]["A"]["pcc_q_mvar"] = [0.2]
        return report

    monkeypatch.setattr(ac_consistency, "validate_ac_dispatch", drift)
    result = solve(ac_tracking_case())
    assert not result.passed and result.recovery.outcome == "unresolved"
    assert all(not a.checks.get("A:q_tracking", True) for a in result.recovery.attempts)


def test_refinement_validation_error_keeps_previous_certified_candidate(monkeypatch):
    original = ac_consistency.validate_ac_dispatch

    def check(case, result, names):
        if (
            result.cluster.get("network_physics") == "branch-flow-equality"
            and result.cluster.get("objective_mode") == "original"
        ):
            raise RuntimeError("injected independent certification failure")
        return original(case, result, names)

    monkeypatch.setattr(ac_consistency, "validate_ac_dispatch", check)
    result = solve(ac_tracking_case())
    assert result.passed and result.recovery.selected_attempt == 1
    assert result.recovery.original_optimality == "unproven"
    assert "certification failed" in result.recovery.attempts[1].reason


def test_disabled_storage_keeps_zero_active_power_during_restoration():
    result = solve(ac_tracking_case(), storage_enabled=False)
    assert result.passed and result.recovery is not None
    d = result.optimization.microgrids["A"]
    np.testing.assert_allclose(d["storage_charge_mw"], 0.0, atol=1e-8)
    np.testing.assert_allclose(d["storage_discharge_mw"], 0.0, atol=1e-8)


def test_restoration_retains_both_reserve_directions_and_terminal_energy():
    case = ac_tracking_case(steps=2)
    reserve = plan_storage_reserve(
        forecast_scale_mw=(7.0, 7.0),
        step_minutes=15,
        remaining_day_minutes=30,
        initial_energy_mwh=2.0,
        minimum_energy_mwh=0.5,
        maximum_energy_mwh=4.0,
        maximum_power_mw=1.0,
        eta_charge=0.95,
        eta_discharge=0.95,
        policy=StorageReservePolicy(),
        required_floor_mw=0.25,
    )
    case = replace(case, storage_reserves={"A": reserve})
    result = solve(case)
    assert result.passed and result.recovery is not None
    data = result.optimization.microgrids["A"]
    p = np.array(data["storage_discharge_mw"]) - np.array(data["storage_charge_mw"])
    e = np.array(data["storage_energy_mwh"])
    assert np.all(p <= np.array(reserve.maximum_power_mw) + 1e-6)
    assert np.all(p >= np.array(reserve.minimum_power_mw) - 1e-6)
    assert np.all(e >= np.array(reserve.energy_floor_mwh) - 1e-6)
    assert np.all(e <= np.array(reserve.energy_ceiling_mwh) + 1e-6)
    assert abs(e[-1] - 2.0) <= 0.05 + 1e-6


@pytest.mark.parametrize("tap", [1.0, 1.01])
def test_exact_physics_uses_same_taps_and_voltage_dependent_fixed_compensation(tap):
    case = ac_tracking_case()
    mg = case.microgrids[0]
    network = mg.network_model_v2
    branch = replace(
        network.branches[0], kind=NetworkBranchKind.TRANSFORMER, fixed_tap_ratio=tap
    )
    shunt = ShuntCompensator("cap", "load", ShuntKind.CAPACITOR, 0.1, 0, 4, 0)
    mode = NetworkOperatingMode(
        "tap-cap",
        "synthetic",
        shunt_operating_points=(ShuntOperatingPoint("cap", True, 2),),
    )
    network = replace(
        network,
        branches=(branch, *network.branches[1:]),
        shunts=(shunt,),
        operating_modes=(mode,),
        default_operating_mode_id=mode.mode_id,
    )
    mg = replace(mg, network_model_v2=network, network_operating_mode_id=mode.mode_id)
    case = replace(case, microgrids=[mg])
    result = solve_case_misocp(
        case,
        exact_ac_physics=True,
        objective_mode="feasibility",
        storage_enabled=False,
        time_limit_seconds=10,
    )
    assert result.success and result.cluster["original_constraints_verified"]
    report = validate_ac_dispatch(case, result, ["A"])
    assert report["passed"]
    assert (
        report["microgrids"]["A"]["maximum_pcc_q_difference_from_optimization_mvar"]
        < 1e-6
    )
    data = result.microgrids["A"]
    np.testing.assert_allclose(
        data["fixed_shunt_q_mvar_by_bus"]["load"],
        0.2 * np.asarray(data["voltage_pu"])[mg.buses.index("load")] ** 2,
        atol=1e-10,
    )


def test_tightened_voltage_infeasibility_is_retried_on_original_domain(monkeypatch):
    actual = ac_consistency.solve_case_misocp

    def backend(*args, **kwargs):
        result = actual(*args, **kwargs)
        if kwargs.get("voltage_security_margin_pu", 0) > 0:
            return replace(result, success=False, message="SCIP status: infeasible")
        return result

    monkeypatch.setattr(ac_consistency, "solve_case_misocp", backend)
    result = solve(ac_tracking_case())
    assert result.passed
    assert result.recovery.trigger == "restricted_model_infeasible"


def test_impossible_original_tracking_still_rejected():
    case = ac_tracking_case()
    target = targets(case)
    target["A"]["p_mw"][:] = -100
    result = solve_case_ac_consistent(
        case, pcc_targets=target, tracking_limits=LIMITS, time_limit_seconds=10
    )
    assert not result.passed and result.stop_reason == "solver_infeasible"


def test_joint_exact_recovery_recomputes_distance_in_physical_model():
    case = ac_tracking_case(steps=2, regions=3)
    target = JointNetworkTarget(
        pf_target=0.95,
        pf_margin_mvar=(0.0, 0.0),
        p_scale_mw=0.05,
        q_scale_mvar=0.05,
        deviation_tolerance=1e-5,
    )
    restored = solve_case_ac_consistent(
        case,
        pcc_targets=targets(case),
        joint_network_target=target,
        p_tracking_penalty_cny_per_mw=0,
        q_tracking_penalty_cny_per_mvar=0,
        relax_and_certify_storage_binaries=False,
        time_limit_seconds=15,
    )
    assert restored.passed and restored.recovery is not None
    evidence = restored.recovery
    assert evidence.attempts[0].certified
    assert evidence.attempts[0].objective == "constant_feasibility"
    assert evidence.attempts[1].objective == "joint_lexicographic"
    # A numerically rejected refinement must retain the physically certified
    # first-phase candidate, never inherit its zero gap as economic evidence.
    if not evidence.attempts[1].certified:
        assert evidence.selected_attempt == 1
        assert evidence.original_optimality == "unproven"
    result = restored.optimization
    assert result.cluster["original_constraints_verified"]
    for m in case.microgrids:
        data = result.microgrids[m.name]
        energy = np.array(data["storage_energy_mwh"])
        ch, dis = (
            np.array(data["storage_charge_mw"]),
            np.array(data["storage_discharge_mw"]),
        )
        np.testing.assert_allclose(
            np.diff(energy), 0.25 * (0.95 * ch - dis / 0.95), atol=1e-6
        )
        assert np.max(np.minimum(ch, dis)) < 1e-7

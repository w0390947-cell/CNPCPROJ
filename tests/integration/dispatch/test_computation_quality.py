"""Real solvers exercise quality budgets independently of UI defaults."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.admm import run_admm_coordination
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.modules.dispatch.contracts import ACRecoveryPolicy, ComputationQualityPolicy
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    RegionalPlanRequest,
    realize_regional_plan,
)
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy
from tests.support.ac_recovery import ac_tracking_case


def test_admm_budget_extension_continues_same_iterates():
    case = build_synthetic_case(steps=4)
    config = ADMMConfig(max_iterations=4, min_iterations=4)
    manual = run_admm_coordination(case, admm_config=config)
    managed = run_admm_coordination(
        case,
        admm_config=replace(
            config,
            max_iterations=1,
            quality_policy=ComputationQualityPolicy(admm_iterations=(1, 2, 4)),
        ),
    )
    assert [b.iteration_budget for b in managed.quality_budgets] == [1, 2, 4]
    assert [b.completed_iterations for b in managed.quality_budgets] == [1, 2, 4]
    assert managed.iterations == manual.iterations == 4
    for name in manual.p_references_mw:
        np.testing.assert_allclose(
            managed.p_references_mw[name], manual.p_references_mw[name], atol=1e-7
        )
    assert not managed.converged  # An exhausted short budget is not a success.


def test_real_scip_timeout_retries_then_reaches_precision_and_ac_checks():
    result = solve_case_ac_consistent(
        build_synthetic_case(steps=4),
        quality_policy=ComputationQualityPolicy(solve_seconds=(1e-9, 30.0)),
    )
    assert result.passed
    quality = result.optimization_quality[-1]
    assert quality.attempts[0].solver_status == "timelimit"
    assert not quality.attempts[0].feasible
    assert quality.status == "satisfied"
    assert quality.attempts[-1].relative_gap <= 1e-4


def test_suboptimal_plan_is_preserved_as_evidence_but_not_adopted(monkeypatch):
    import oilfield_energy.bootstrap.adapters.cluster_execution as adapter

    case = build_synthetic_case(steps=4)
    checked = solve_case_ac_consistent(case, quality_policy=ComputationQualityPolicy())
    quality = checked.optimization_quality[-1].model_copy(
        update={"status": "budget_exhausted"}
    )
    checked = replace(checked, optimization_quality=(quality,))
    monkeypatch.setattr(adapter, "solve_case_ac_consistent", lambda *a, **kw: checked)
    local = checked.optimization.microgrids["SC"]
    evidence, adopted = realize_regional_plan(
        RegionalPlanRequest(
            case=case,
            name="SC",
            p_target_mw=local["p_grid_mw"],
            q_target_mvar=local["q_grid_mvar"],
            storage_enabled=True,
            seconds=180,
            policy=ExecutionPolicy(computation_quality=ComputationQualityPolicy()),
        )
    )
    assert adopted is None
    assert evidence.optimization_quality[-1].status == "budget_exhausted"
    assert len(evidence.p_mw) == 4
    assert (
        next(c for c in evidence.checks if c.code == "OPTIMIZATION_QUALITY").status
        == "unknown"
    )


@pytest.fixture(scope="module")
def feasible_single_solve():
    case = ac_tracking_case()
    checked = solve_case_ac_consistent(
        case, ["A"], storage_enabled=False, time_limit_seconds=10
    )
    assert checked.passed and checked.recovery is None
    return case, checked.optimization


@pytest.mark.parametrize(
    "termination,gap,adopted",
    [
        ("timelimit", 0.2, False),
        ("optimal", None, False),
        ("optimal", float("nan"), False),
        ("optimal", float("inf"), False),
        ("optimal", -1.0, False),
        ("optimal", 0.0, True),
        ("gaplimit", 1e-4, True),
        ("timelimit", 1e-4, True),
    ],
)
def test_single_budget_still_requires_optimality_evidence(
    monkeypatch, feasible_single_solve, termination, gap, adopted
):
    from oilfield_energy import ac_consistency

    case, physical = feasible_single_solve
    incumbent = replace(
        physical, message=f"SCIP status: {termination}", mip_gap=gap
    )
    calls = []

    def backend(*args, **kwargs):
        calls.append(kwargs)
        return incumbent

    monkeypatch.setattr(ac_consistency, "solve_case_misocp", backend)
    local = physical.microgrids["A"]
    evidence, plan = realize_regional_plan(
        RegionalPlanRequest(
            case, "A", local["p_grid_mw"], local["q_grid_mvar"], False, 10,
            ExecutionPolicy(ac_recovery=ACRecoveryPolicy(allow_feasible_adoption=False)),
        )
    )
    assert len(calls) == 1
    assert calls[0]["relative_gap"] == 1e-4
    assert evidence.ac_recovery is None and evidence.adoption_basis == "standard"
    assert (plan is not None) == adopted
    assert evidence.executable_plan == adopted
    check = next(c for c in evidence.checks if c.code == "OPTIMIZATION_QUALITY")
    assert check.status == ("passed" if adopted else "unknown")
    assert check.limit == 1e-4
    if gap is None or not np.isfinite(gap) or gap < 0:
        assert check.actual is None
    else:
        assert check.actual == gap

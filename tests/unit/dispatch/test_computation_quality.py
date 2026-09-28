"""Independent budget/quality and failure-path contracts, without solver SDKs."""

import pytest

from oilfield_energy.modules.dispatch.api import solve_for_quality
from oilfield_energy.modules.dispatch.contracts import (
    ComputationQualityPolicy,
    OptimizationAttempt,
)


@pytest.mark.parametrize(
    "status", ["infeasible", "unbounded", "userinterrupt", "unknown", "numeric_error"]
)
def test_non_budget_stops_never_retry(status):
    calls = []

    def solve(seconds):
        calls.append(seconds)
        return None, OptimizationAttempt(
            budget_seconds=seconds, solver_status=status, feasible=False
        )

    _, evidence = solve_for_quality(solve, ComputationQualityPolicy())
    assert calls == [180]
    assert evidence.status == ("infeasible" if status == "infeasible" else "stopped")


def test_only_budget_exhaustion_retries_and_quality_stops_early():
    calls = []

    def solve(seconds):
        calls.append(seconds)
        return len(calls), OptimizationAttempt(
            budget_seconds=seconds,
            solver_status="timelimit",
            feasible=True,
            relative_gap=0.1 if len(calls) == 1 else 1e-5,
        )

    value, evidence = solve_for_quality(solve, ComputationQualityPolicy())
    assert calls == [180, 600]
    assert value == 2 and evidence.status == "satisfied"


def test_exhaustion_retains_available_solution_and_does_not_claim_optimality():
    calls = []

    def solve(seconds):
        calls.append(seconds)
        feasible = len(calls) == 1
        return len(calls), OptimizationAttempt(
            budget_seconds=seconds,
            solver_status="timelimit",
            feasible=feasible,
            relative_gap=0.1 if feasible else None,
        )

    value, evidence = solve_for_quality(solve, ComputationQualityPolicy())
    assert calls == [180, 600, 1800]
    assert value == evidence.selected_attempt == 1
    assert evidence.status == "budget_exhausted"


def test_cancellation_and_sdk_exceptions_propagate_without_retry():
    def solve(_seconds):
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        solve_for_quality(solve, ComputationQualityPolicy())


def test_worse_retry_does_not_replace_a_better_feasible_candidate():
    costs = iter([10.0, 11.0, 12.0])

    def solve(seconds):
        cost = next(costs)
        return cost, OptimizationAttempt(
            budget_seconds=seconds,
            solver_status="timelimit",
            feasible=True,
            relative_gap=0.1,
            objective_cny=cost,
        )

    value, evidence = solve_for_quality(solve, ComputationQualityPolicy())
    assert value == 10.0 and evidence.selected_attempt == 1
    assert evidence.status == "budget_exhausted"


def test_missing_gap_cannot_become_a_quality_certificate():
    _, evidence = solve_for_quality(
        lambda s: (
            None,
            OptimizationAttempt(
                budget_seconds=s, solver_status="optimal", feasible=True
            ),
        ),
        ComputationQualityPolicy(),
    )
    assert evidence.status == "stopped"


def test_numerical_stop_after_timeout_is_not_mislabeled_as_budget_exhaustion():
    statuses = iter(["timelimit", "numeric_error"])
    _, evidence = solve_for_quality(
        lambda s: (
            None,
            OptimizationAttempt(
                budget_seconds=s, solver_status=next(statuses), feasible=False
            ),
        ),
        ComputationQualityPolicy(),
    )
    assert len(evidence.attempts) == 2
    assert evidence.status == "stopped"


@pytest.mark.parametrize("values", [(), (2, 1), (0, 2), (1801,), (float("nan"),)])
def test_invalid_budgets_rejected(values):
    with pytest.raises(ValueError):
        ComputationQualityPolicy(solve_seconds=values)

"""Restoration budget/selection rules use injected clocks and numerical ports."""

import pytest

from oilfield_energy.modules.dispatch.api import run_ac_feasibility_recovery
from oilfield_energy.modules.dispatch.contracts import (
    ACRecoveryAttempt,
    ACRecoveryPolicy,
)


def attempt(phase, seconds, *, certified=True, cost=20.0, gap=0.0, scope="none"):
    return ACRecoveryAttempt(
        phase=phase,
        budget_seconds=seconds,
        elapsed_seconds=0.0,
        objective="constant_feasibility"
        if phase == "physical_feasibility"
        else "original_economic",
        solver_status="optimal" if certified else "timelimit",
        solver_feasible=certified,
        certified=certified,
        checks={"independent_physics": certified},
        original_objective_cny=cost if certified else None,
        solver_objective_value=0.0,
        relative_gap=gap,
        infeasibility_scope=scope,
    )


def test_zero_feasibility_gap_is_not_economic_optimality_and_failed_refinement_keeps_candidate():
    def solve(phase, seconds):
        ok = phase == "physical_feasibility"
        return "certified_plan" if ok else None, attempt(phase, seconds, certified=ok)

    result = run_ac_feasibility_recovery(
        policy=ACRecoveryPolicy(),
        trigger="exhausted",
        budget_seconds=10,
        solve=solve,
        clock=lambda: 0.0,
    )
    assert result.candidate == "certified_plan"
    assert result.evidence.selected_attempt == 1
    assert result.evidence.original_optimality == "unproven"
    assert result.evidence.permits_feasible_adoption


def test_common_budget_can_expire_after_feasible_candidate_without_losing_it():
    now = [0.0]

    def solve(phase, seconds):
        assert seconds == 4.0
        now[0] = 12.0  # Includes validation/solver overrun; no new solve afterwards.
        return "plan", attempt(phase, seconds)

    result = run_ac_feasibility_recovery(
        policy=ACRecoveryPolicy(),
        trigger="exhausted",
        budget_seconds=10,
        solve=solve,
        clock=lambda: now[0],
    )
    assert result.candidate == "plan" and len(result.evidence.attempts) == 1
    assert result.evidence.elapsed_seconds == 12.0


def test_candidate_compared_using_original_objective_not_zero_feasibility_objective():
    def solve(phase, seconds):
        cost = 20.0 if phase == "physical_feasibility" else 10.0
        return phase, attempt(phase, seconds, cost=cost)

    result = run_ac_feasibility_recovery(
        policy=ACRecoveryPolicy(),
        trigger="exhausted",
        budget_seconds=10,
        solve=solve,
        clock=lambda: 0.0,
    )
    assert result.candidate == "original_objective"


@pytest.mark.parametrize(
    "scope,outcome",
    [
        ("original_model", "model_infeasible"),
        ("restricted_model", "unresolved"),
        ("none", "unresolved"),
    ],
)
def test_auxiliary_infeasibility_and_timeout_do_not_prove_original_infeasibility(
    scope, outcome
):
    def solve(phase, seconds):
        return None, attempt(phase, seconds, certified=False, scope=scope)

    result = run_ac_feasibility_recovery(
        policy=ACRecoveryPolicy(),
        trigger="exhausted",
        budget_seconds=10,
        solve=solve,
        clock=lambda: 0.0,
    )
    assert result.evidence.outcome == outcome
    assert result.candidate is None


def test_cancellation_propagates():
    def solve(phase, seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_ac_feasibility_recovery(
            policy=ACRecoveryPolicy(),
            trigger="exhausted",
            budget_seconds=10,
            solve=solve,
            clock=lambda: 0.0,
        )

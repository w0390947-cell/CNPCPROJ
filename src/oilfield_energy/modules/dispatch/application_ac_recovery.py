"""Bounded restoration with injected solver/certifier and monotonic clock ports."""

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Generic, TypeVar

from .contracts_ac_recovery import (
    ACRecoveryAttempt,
    ACRecoveryEvidence,
    ACRecoveryPhase,
    ACRecoveryPolicy,
)

T = TypeVar("T")


@dataclass(frozen=True)
class ACRecoverySelection(Generic[T]):
    candidate: T | None
    evidence: ACRecoveryEvidence


def run_ac_feasibility_recovery(
    *,
    policy: ACRecoveryPolicy,
    trigger: str,
    budget_seconds: float,
    solve: Callable[[ACRecoveryPhase, float], tuple[T | None, ACRecoveryAttempt]],
    clock: Callable[[], float],
) -> ACRecoverySelection[T]:
    """A failed refinement never discards a previously certified incumbent.

    The numerical port must rebuild the ORIGINAL hard domain, with exact branch
    physics and integer storage, and independently certify each proposed plan.
    No SDK, clock access, case mutation or physical-state advancement lives here.
    """
    if not isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("restoration requires a positive finite budget")
    budget = min(budget_seconds, policy.maximum_seconds)
    started = clock()
    attempts: list[ACRecoveryAttempt] = []
    selected: tuple[T, int] | None = None
    phases: tuple[ACRecoveryPhase, ...] = ("physical_feasibility", "original_objective")
    for phase in phases:
        remaining = budget - max(0.0, clock() - started)
        if remaining <= 0:
            break
        seconds = (
            min(remaining, budget * policy.feasibility_fraction)
            if not attempts
            else remaining
        )
        candidate, row = solve(phase, seconds)
        if row.phase != phase or row.budget_seconds != seconds:
            raise ValueError(
                "restoration observation differs from its requested phase/budget"
            )
        attempts.append(row)
        if row.certified:
            if candidate is None:
                raise ValueError("a certificate requires its actual candidate")
            prior = attempts[selected[1] - 1] if selected is not None else None
            assert row.original_objective_cny is not None
            # Joint restoration ranks distance first, economic objective second.
            # Each objective value evaluates the original algebraic objective;
            # feasibility's zero solver objective is never used for comparison.
            score = (row.reference_distance or 0.0, row.original_objective_cny)
            prior_score = (
                (
                    prior.reference_distance or 0.0,
                    prior.original_objective_cny
                    if prior.original_objective_cny is not None
                    else float("inf"),
                )
                if prior
                else None
            )
            if prior_score is None or score <= prior_score:
                selected = candidate, len(attempts)
        if row.infeasibility_scope == "original_model":
            break
    record = attempts[selected[1] - 1] if selected is not None else None
    evidence = ACRecoveryEvidence(
        policy=policy,
        trigger=trigger,
        budget_seconds=budget,
        elapsed_seconds=max(0.0, clock() - started),
        attempts=tuple(attempts),
        selected_attempt=selected[1] if selected else None,
        outcome="certified"
        if selected
        else "model_infeasible"
        if any(a.infeasibility_scope == "original_model" for a in attempts)
        else "unresolved",
        original_optimality="proven"
        if record and record.original_optimality_proven
        else "unproven",
    )
    return ACRecoverySelection(selected[0] if selected else None, evidence)

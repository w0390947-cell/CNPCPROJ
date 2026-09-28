"""Bounded retries for budget-limited optimization, without changing its objective."""

from collections.abc import Callable
from typing import TypeVar

from .contracts import (
    ComputationQualityPolicy,
    OptimizationAttempt,
    OptimizationQuality,
)

T = TypeVar("T")


def solve_for_quality(
    solve: Callable[[float], tuple[T, OptimizationAttempt]],
    policy: ComputationQualityPolicy,
) -> tuple[T, OptimizationQuality]:
    """Retry only time exhaustion; retain a feasible candidate if a later run fails.

    Each call is an independent solve with the same model/precision. ADMM uses
    continuation instead. Exceptions (including cancellation) propagate unchanged.
    """
    attempts: list[OptimizationAttempt] = []
    selected: tuple[T, int] | None = None
    status = "stopped"
    for seconds in policy.solve_seconds:
        value, observation = solve(seconds)
        if observation.budget_seconds != seconds:
            raise ValueError("solver evidence does not match the requested budget")
        attempts.append(observation)
        prior = attempts[selected[1] - 1] if selected is not None else None
        if selected is None or (
            observation.feasible
            and (
                prior is None
                or not prior.feasible
                or (
                    observation.objective_cny is not None
                    and (
                        prior.objective_cny is None
                        or observation.objective_cny < prior.objective_cny
                    )
                )
            )
        ):
            selected = value, len(attempts)
        if (
            observation.feasible
            and observation.relative_gap is not None
            and observation.relative_gap <= policy.relative_gap
            and observation.solver_status in {"optimal", "gaplimit", "timelimit"}
        ):
            selected = value, len(attempts)
            status = "satisfied"
            break
        if observation.solver_status == "infeasible":
            status = "stopped" if prior is not None and prior.feasible else "infeasible"
            break
        if observation.solver_status != "timelimit":
            status = "stopped"
            break
        status = "budget_exhausted"
    assert selected is not None
    return selected[0], OptimizationQuality(
        status=status,
        target_relative_gap=policy.relative_gap,
        attempts=tuple(attempts),
        selected_attempt=selected[1],
    )

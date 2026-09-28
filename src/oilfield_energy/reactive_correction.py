"""Bounded corrective Q dispatch on a validated, fixed-active AC snapshot.

Searches command targets, not fictitious aggregate injections. The caller's
pure projection includes subcontroller arbitration; every candidate includes
the same lag and aggregate ramp as execution. No command is sent here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np
from scipy.optimize import minimize

from .ac_power_flow import backward_forward_sweep_resolved
from .network_model import ResolvedNetwork
from .network_scenarios import NetworkSecurityLimits
from .reactive_execution import ReactiveCapability
from .modules.control.api import track_reactive_power
from .modules.control.contracts import ReactiveTrackingPoint, ReactiveTrackingResult


def reactive_response(current, targets, *, alpha: float, ramp_mvar: float):
    """One common response model for prediction and actual SIL execution."""
    change = alpha * (np.asarray(targets) - np.asarray(current))
    movement = float(np.sum(np.abs(change)))
    if ramp_mvar <= 0:
        change = np.zeros_like(change)
    elif movement > ramp_mvar:
        change *= ramp_mvar / movement
    return np.asarray(current) + change


@dataclass(frozen=True)
class ReactiveCorrection:
    controls: np.ndarray
    targets: np.ndarray
    predicted_actual: np.ndarray
    status: str
    evaluations: int
    minimum_constraint_margin: float
    tracking: ReactiveTrackingResult | None = None


def correct_reactive_dispatch(
    network: ResolvedNetwork,
    limits: NetworkSecurityLimits,
    *,
    p_demand_mw: np.ndarray,
    q_demand_before_mvar: np.ndarray,
    resource_buses: Sequence[str],
    current_q: np.ndarray,
    preferred_targets: np.ndarray,
    capabilities: Sequence[ReactiveCapability],
    project_targets: Callable[[np.ndarray], np.ndarray],
    slack_voltage_pu: float,
    alpha: float,
    ramp_mvar: float,
    max_iterations: int = 60,
    pcc_target_mvar: float | None = None,
) -> ReactiveCorrection:
    """Certify a one-step action, or return an improving bounded action.

    ``q_demand_before_mvar`` contains net bus demand at ``current_q``. Shunts
    remain in the AC model. A failed feasibility search is not an infeasibility
    proof: best_effort means this step remains unsafe, with that failure kept.
    """
    current = np.asarray(current_q, dtype=float)
    preferred = np.asarray(preferred_targets, dtype=float)
    lower = np.array([c.minimum_mvar for c in capabilities])
    upper = np.array([c.maximum_mvar for c in capabilities])
    n = len(capabilities)
    if (current.shape != (n,) or preferred.shape != (n,)
            or len(resource_buses) != n or n == 0
            or not np.all(np.isfinite(current)) or not np.all(np.isfinite(preferred))
            or not 0 < alpha <= 1 or not np.isfinite(ramp_mvar) or ramp_mvar < 0
            or type(max_iterations) is not int or max_iterations < 1):
        raise ValueError("invalid corrective reactive-dispatch inputs")
    if np.any(current < lower - 1e-8) or np.any(current > upper + 1e-8):
        raise ValueError("current Q must be within current capability")
    bus_ids = [b.bus_id for b in network.buses]
    incidence = np.array([[float(b == rb) for rb in resource_buses] for b in bus_ids])
    if not set(resource_buses).issubset(bus_ids):
        raise ValueError("reactive resource bus is missing from network")
    # Small control reserve, distinct from comparison tolerances or the 0.92
    # economic/planning target. Never relax the configured safety floor.
    pf_target = min(1., limits.power_factor_min + 1e-5)
    pf_tan = float(np.tan(np.arccos(pf_target)))
    voltage_reserve = np.full(len(bus_ids), 1e-6)
    voltage_reserve[bus_ids.index(network.pcc_bus_id)] = 0.
    count = 2 * len(bus_ids) + len(network.branches) + 6
    evaluations = 0
    cache: dict[bytes, tuple] = {}
    pcc_cache: dict[bytes, float] = {}

    def margins(actual):
        nonlocal evaluations
        evaluations += 1
        try:
            flow = backward_forward_sweep_resolved(
                network, p_demand_mw,
                np.asarray(q_demand_before_mvar) - incidence @ (actual - current),
                slack_voltage_pu=slack_voltage_pu,
                tolerance=limits.flow_tolerance, max_iterations=limits.max_iterations,
            )
        except (ValueError, FloatingPointError, OverflowError):
            return np.full(count, -1e6)
        if not flow["converged"]:
            return np.full(count, -1e6)
        p, q = flow["pcc_p_mw"], flow["pcc_q_mvar"]
        pcc_cache[np.asarray(actual, dtype=float).tobytes()] = float(q)
        # Scale voltage residuals so sub-milliper-unit errors are not ignored
        # next to Mvar residuals. Both terminals are included by the AC solver.
        return np.r_[1000 * (flow["voltage_pu"] - limits.voltage_min_pu - voltage_reserve),
                     1000 * (limits.voltage_max_pu - flow["voltage_pu"] - voltage_reserve),
                     1 - flow["line_loading_pu"],
                     p - max(0., limits.pcc_import_min_mw),
                     limits.pcc_import_max_mw - p, pf_tan * p - q, pf_tan * p + q, 0., 0.]

    def evaluate(controls):
        controls = np.asarray(controls, dtype=float)
        if controls.shape != current.shape or not np.all(np.isfinite(controls)):
            return current.copy(), current.copy(), np.full(count, -1e6)
        controls = np.clip(controls, lower, upper)
        key = controls.tobytes()
        if key not in cache:
            targets = np.asarray(project_targets(controls.copy()), dtype=float)
            if (targets.shape != current.shape or not np.all(np.isfinite(targets))
                    or np.any(targets < lower - 1e-8) or np.any(targets > upper + 1e-8)):
                cache[key] = (current.copy(), current.copy(), np.full(count, -1e6))
            else:
                actual = reactive_response(current, targets, alpha=alpha, ramp_mvar=ramp_mvar)
                constraints = margins(actual)
                # A rejected/clipped group request must be reassigned upstream,
                # not hidden by a physically safe but unserved aggregate target.
                residual = float(np.sum(targets) - np.sum(controls))
                constraints[-2:] = (residual, -residual)
                cache[key] = (targets, actual, constraints)
        return cache[key]

    def output(controls, status):
        targets, actual, constraints = evaluate(controls)
        tracking = None
        if pcc_target_mvar is not None and status in ("safe", "corrected"):
            def point(commands):
                projected, response, margins_ = evaluate(np.asarray(commands))
                return ReactiveTrackingPoint(tuple(map(float, commands)), tuple(map(float, projected)),
                    tuple(map(float, response)), pcc_cache.get(response.tobytes()), bool(np.min(margins_) >= -1e-8))
            tracking = track_reactive_power(
                initial=point(tuple(map(float, np.clip(controls, lower, upper)))),
                minimum_mvar=tuple(map(float, lower)), maximum_mvar=tuple(map(float, upper)),
                pcc_target_mvar=pcc_target_mvar, alpha=alpha, evaluate=point,
            )
            controls = np.asarray(tracking.point.controls)
            targets, actual, constraints = evaluate(controls)
        return ReactiveCorrection(np.clip(controls, lower, upper), targets.copy(),
                                  actual.copy(), status, evaluations, float(np.min(constraints)), tracking)

    x0 = np.clip(preferred, lower, upper)
    if np.min(evaluate(x0)[2]) >= -1e-8:
        return output(x0, "safe")
    # An impossible fixed slack or active-only overload cannot be cured by Q.
    # The conservative fallback still allows progress toward attainable limits.
    initial = margins(current)
    if np.min(initial) <= -1e5:
        return ReactiveCorrection(current.copy(), current.copy(), current.copy(),
                                  "unavailable", evaluations, float(np.min(initial)))
    if not limits.voltage_min_pu <= slack_voltage_pu <= limits.voltage_max_pu:
        return ReactiveCorrection(current.copy(), current.copy(), current.copy(),
                                  "fixed_slack_infeasible", evaluations, float(np.min(initial)))
    children = {b: [] for b in bus_ids}
    for branch in network.branches:
        children[branch.parent].append(branch)
    active = dict(zip(bus_ids, np.asarray(p_demand_mw), strict=True))
    def subtree(bus):
        # With receiving-terminal S <= Smax and V >= Vmin, every descendant
        # real loss is in [0, r*Smax^2/(base_mva*Vmin^2)]. The resulting P
        # interval is a necessary condition, independent of Q allocation.
        low, high, impossible = float(active[bus]), float(active[bus]), False
        for branch in children[bus]:
            child_low, child_high, child_bad = subtree(branch.child)
            impossible |= (child_bad or child_low > branch.s_max_mva + 1e-8
                           or child_high < -branch.s_max_mva - 1e-8)
            low += child_low
            high += child_high + branch.r_pu * branch.s_max_mva**2 / (
                network.base_mva * limits.voltage_min_pu**2)
        return low, high, impossible
    if subtree(network.pcc_bus_id)[2]:
        return ReactiveCorrection(current.copy(), current.copy(), current.copy(),
                                  "fixed_active_infeasible", evaluations, float(np.min(initial)))
    movable = np.flatnonzero(upper - lower > 1e-12)
    if not len(movable) or ramp_mvar == 0:
        return ReactiveCorrection(current.copy(), current.copy(), current.copy(),
                                  "held", evaluations, float(np.min(initial)))

    def expand(x):
        value = lower.copy()
        value[movable] = x
        return value

    def movement(x):
        return float(np.sum((evaluate(expand(x))[1] - current) ** 2))

    bounds = [(lower[i], upper[i]) for i in movable]
    # Each search evaluates the *projected* and lagged actual injections.
    result = minimize(movement, x0[movable], method="SLSQP", bounds=bounds,
                      constraints={"type": "ineq", "fun": lambda x: evaluate(expand(x))[2]},
                      options={"maxiter": max_iterations, "ftol": 1e-11})
    candidate = expand(result.x)
    if np.all(np.isfinite(candidate)) and np.min(evaluate(candidate)[2]) >= -1e-8:
        return output(candidate, "corrected")

    # If a safe step was not found, reduce the existing violations while
    # preserving every formerly satisfied constraint. Do not label this safe
    # or physically infeasible merely because SLSQP failed to certify a step.
    floor = np.minimum(initial, 0.)
    def violation_objective(x):
        deficit = np.minimum(evaluate(expand(x))[2], 0.)
        return float(deficit @ deficit + 1e-7 * movement(x))

    best = minimize(violation_objective, np.clip(current, lower, upper)[movable],
                    method="SLSQP", bounds=bounds,
                    constraints={"type": "ineq", "fun": lambda x: evaluate(expand(x))[2] - floor},
                    options={"maxiter": max_iterations, "ftol": 1e-11})
    candidate = expand(best.x)
    constraints = evaluate(candidate)[2]
    if (np.all(np.isfinite(candidate)) and np.min(constraints - floor) >= -1e-8
            and np.sum(np.minimum(constraints, 0.) ** 2)
            < np.sum(np.minimum(initial, 0.) ** 2) - 1e-12):
        return output(candidate, "corrected" if np.min(constraints) >= -1e-8 else "best_effort")
    return ReactiveCorrection(current.copy(), current.copy(), current.copy(),
                              "held", evaluations, float(np.min(initial)))

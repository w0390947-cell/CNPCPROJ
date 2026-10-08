"""Finite target search and economic selection under the common certificate."""

from math import acos, tan
from typing import Literal

from oilfield_energy.modules.control.api import point_box
from oilfield_energy.modules.control.contracts import TubeCommand, TubeState

from .contracts_unified import TargetAdmission, UnifiedDomain, UnifiedPlanCertificate
from .economics import admitted_policy_cost
from .tracking import certify_tracking_target, total_load, tracking_goals


def admit_tracking_target(
    domain: UnifiedDomain,
    state: TubeState,
    start: int,
    end: int,
    requested: tuple[float, float],
    nominal: TubeCommand,
    origin: Literal["economic", "external", "retained"],
    previous_target: tuple[float, float] | None,
    previous_deadline: int,
    *,
    require_task: bool = True,
) -> TargetAdmission:
    """Search explicit nearby internal targets; never rewrite external targets."""
    targets = [requested]
    if (
        origin == "economic"
        and domain.network.pcc_min_mw <= requested[0] <= domain.network.pcc_max_mw
    ):
        goal, _, q_reg = tracking_goals(domain, start, end, nominal)
        other_q = sum(q for i, q in enumerate(goal.q_mvar) if i != q_reg)
        q_reference = (
            domain.devices.resources[q_reg].q.high
            * domain.research.reactive_regulator_reference_fraction
        )
        q = max(
            requested[1],
            max(total_load(domain, k, True).high for k in range(start, end))
            - other_q
            - q_reference
            + 0.04,
        )
        renewable_p = sum(
            p
            for p, r in zip(goal.p_mw, domain.devices.resources, strict=True)
            if r.kind in ("wind", "pv")
        )
        hints = (
            max(total_load(domain, k, False).high for k in range(start, end))
            - renewable_p
            + 0.07,
            min(total_load(domain, k, False).low for k in range(start, end))
            - renewable_p
            - 0.03,
        )
        task = domain.energy_task
        if task is not None and start < task.deadline_step:
            storage = next(r for r in domain.devices.resources if r.kind == "storage")
            error = state.energy_mwh - (task.energy_mwh.low + task.energy_mwh.high) / 2
            hours = (task.deadline_step - start) * domain.devices.step_minutes / 60
            power = (
                error * domain.devices.eta_discharge / hours
                if error >= 0
                else error / domain.devices.eta_charge / hours
            )
            power = min(storage.p.high, max(storage.p.low, power))
            demand = sum(
                (total_load(domain, k, False).low + total_load(domain, k, False).high)
                / 2
                for k in range(start, end)
            ) / (end - start)
            p = demand - renewable_p - power + 0.025
            if domain.network.pcc_min_mw <= p <= domain.network.pcc_max_mw:
                targets.extend(
                    candidate
                    for candidate in ((p, requested[1]), (p, q))
                    if candidate not in targets
                )
        for p in ((hints[0] + hints[1]) / 2, *hints):
            if domain.network.pcc_min_mw <= p <= domain.network.pcc_max_mw:
                targets.extend(
                    candidate
                    for candidate in ((p, requested[1]), (p, q))
                    if candidate not in targets
                )
        for shift in (
            0,
            0.15,
            -0.15,
            0.3,
            -0.3,
            0.45,
            -0.45,
            0.6,
            -0.6,
            0.75,
            -0.75,
            1,
            -1,
            1.5,
            -1.5,
            2,
            -2,
            3,
            -3,
        ):
            p = requested[0] + shift
            if domain.network.pf_min < 1:
                p = max(p, abs(q) / tan(acos(domain.network.pf_min)) + 0.02)
            if domain.network.pcc_min_mw <= p <= domain.network.pcc_max_mw:
                for candidate in ((p, requested[1]), (p, q)):
                    if candidate not in targets:
                        targets.append(candidate)
    attempted: list[tuple[float, float]] = []
    costs: list[float | None] = []
    outcomes: list[Literal["certified", "construction_unproven"]] = []
    reasons: list[str] = []
    candidates: list[tuple[int, UnifiedPlanCertificate]] = []
    reason = "target admission did not obtain a robust witness"
    for target in targets:
        attempted.append(target)
        deadline = (
            previous_deadline
            if target == previous_target
            else start + domain.response_steps
        )
        try:
            plan = certify_tracking_target(
                domain,
                point_box(state),
                start,
                end,
                deadline,
                target,
                nominal,
                require_task=require_task,
            )
            cost = admitted_policy_cost(domain, plan)
        except ValueError as exc:
            reason = str(exc)
            costs.append(None)
            outcomes.append("construction_unproven")
            reasons.append(reason)
        else:
            costs.append(cost)
            outcomes.append("certified")
            reasons.append("")
            candidates.append((len(attempted) - 1, plan))
            if (
                target == requested
                or domain.economic_rates is None
                or len(candidates) >= domain.research.certified_economic_candidates
            ):
                break
    if candidates:
        selected, plan = min(
            candidates,
            key=lambda item: costs[item[0]] or 0.0,
        )
        return TargetAdmission(
            start_step=start,
            end_step=end,
            origin=origin,
            requested_p_mw=requested[0],
            requested_q_mvar=requested[1],
            nominal=nominal,
            attempted_targets=tuple(attempted),
            plan=plan,
            selected_candidate=selected,
            candidate_economic_costs_cny=tuple(costs),
            candidate_outcomes=tuple(outcomes),
            candidate_reasons=tuple(reasons),
        )
    return TargetAdmission(
        start_step=start,
        end_step=end,
        origin=origin,
        requested_p_mw=requested[0],
        requested_q_mvar=requested[1],
        nominal=nominal,
        attempted_targets=tuple(attempted),
        reason=reason,
        candidate_economic_costs_cny=tuple(costs),
        candidate_outcomes=tuple(outcomes),
        candidate_reasons=tuple(reasons),
    )

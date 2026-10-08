"""Finite robust target admission, with causal load feedback and safe continuation."""

from fractions import Fraction as F
from functools import lru_cache
from hashlib import sha256

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.modules.control.api import (
    approach_backup_command,
    certify_balance_policy,
    validate_device_box,
)
from oilfield_energy.modules.control.contracts import (
    BalancePolicy,
    TubeBox,
    TubeCommand,
    TubeState,
)
from oilfield_energy.modules.power_flow.api import (
    certified_ac_loss_bounds,
    refine_ac_balance_certificate,
)
from oilfield_energy.modules.power_flow.contracts import PowerInterval

from .allocation import (
    allocate_goal,
    allocation_options,
    future_model,
    intervention_score,
    participation,
)
from .certificates import (
    certify_unified_plan,
    device_model_at,
    forecast_at,
    unified_network_box,
)
from .contracts_unified import (
    AllocationTrial,
    TargetTrackingCertificate,
    UnifiedDomain,
    UnifiedPlanCertificate,
)
from .economics import admitted_policy_cost
from .task_allocation import terminal_task_allocation


def total_load(domain: UnifiedDomain, step: int, reactive: bool) -> PowerInterval:
    values = tuple(
        b.q_mvar if reactive else b.p_mw for b in forecast_at(domain, step).loads
    )
    return PowerInterval(
        low=lower(sum((F(x.low) for x in values), F(0))),
        high=upper(sum((F(x.high) for x in values), F(0))),
    )


def _point(box: TubeBox) -> TubeState:
    return TubeState(
        p_mw=tuple((x.low + x.high) / 2 for x in box.p_mw),
        q_mvar=tuple((x.low + x.high) / 2 for x in box.q_mvar),
        energy_mwh=(box.energy_mwh.low + box.energy_mwh.high) / 2,
    )


def tracking_goals(
    domain: UnifiedDomain,
    start: int,
    end: int,
    nominal: TubeCommand,
    regulators: tuple[int, int] | None = None,
):
    resources = domain.devices.resources
    storage = next(i for i, r in enumerate(resources) if r.kind == "storage")
    p_reg = (
        storage
        if resources[storage].p.high > resources[storage].p.low
        else next(
            (
                i
                for i, r in enumerate(resources)
                if r.kind in ("wind", "pv") and r.p.high > r.p.low
            ),
            None,
        )
    )
    q_reg = next(
        (i for i, r in enumerate(resources) if r.kind == "svg" and r.q.high > r.q.low),
        None,
    )
    if q_reg is None:
        q_reg = next((i for i, r in enumerate(resources) if r.q.high > r.q.low), None)
    if p_reg is None or q_reg is None:
        raise ValueError(
            "this constructive tracking policy requires P and Q regulators"
        )
    if regulators is not None:
        p_reg, q_reg = regulators
    # Economic solver tolerances are not physical command permissions.
    # Normalize reference goals to exact actuator intervals before motion;
    # requested PCC targets are still admitted separately without relaxation.
    ps = [
        min(r.p.high, max(r.p.low, p))
        for r, p in zip(resources, nominal.p_mw, strict=True)
    ]
    qs = [
        min(r.q.high, max(r.q.low, q))
        for r, q in zip(resources, nominal.q_mvar, strict=True)
    ]
    for i, r in enumerate(resources):
        if r.kind in ("wind", "pv"):
            ceiling = min(
                device_model_at(domain, k).resources[i].p.high
                for k in range(start, end + 1)
            )
            ps[i] = max(
                0,
                min(
                    ps[i],
                    ceiling
                    - min(domain.research.renewable_command_reserve_mw, ceiling / 10),
                ),
            )
            if i == p_reg:
                ps[i] = ceiling / 2
                qs[i] = 0
            if r.q_over_p is not None:
                cap = max(0, r.q_over_p * ps[i] - 1e-5)
                qs[i] = max(-cap, min(qs[i], cap))
    return TubeCommand(p_mw=tuple(ps), q_mvar=tuple(qs)), p_reg, q_reg


def _certify_strategy(
    domain: UnifiedDomain,
    initial: TubeBox,
    start: int,
    end: int,
    deadline: int,
    target: tuple[float, float],
    nominal: TubeCommand,
    *,
    require_task: bool = True,
    construction: int = 0,
) -> UnifiedPlanCertificate:
    """All-loads feedback witness until end, followed by certified safe return.

    The tracking promise has a finite expiry. A missed update never changes
    the original target/deadline and never removes its execution evaluations.
    """
    if end <= start or not start < deadline <= end and deadline > start:
        raise ValueError("tracking horizon does not cover its deadline")
    _, preferred_p, preferred_q = tracking_goals(domain, start, end, nominal)
    p_reg, q_reg, shared, dynamic = allocation_options(
        domain, (preferred_p, preferred_q)
    )[construction]
    goal, _, _ = tracking_goals(domain, start, end, nominal, (p_reg, q_reg))
    goal = terminal_task_allocation(domain, initial, start, end, target[0], goal, p_reg)
    # Reach the balancing regulator's operating point before its promise starts.
    # Approaching the economic PCS extreme instead can leave a full-power jump
    # at the deadline even when the eventual balancing target is reachable.
    first_hold = max(start, deadline - 1)
    dp, dq = total_load(domain, first_hold, False), total_load(domain, first_hold, True)
    ps, qs = list(goal.p_mw), list(goal.q_mvar)
    p_goal = (
        (dp.low + dp.high) / 2
        - sum(p for i, p in enumerate(ps) if i != p_reg)
        - target[0]
        + 0.025
    )
    q_goal = (
        (dq.low + dq.high) / 2
        - sum(q for i, q in enumerate(qs) if i != q_reg)
        - target[1]
        + 0.04
    )
    r = domain.devices.resources[p_reg]
    p_high = min(
        device_model_at(domain, k).resources[p_reg].p.high
        for k in range(start, end + 1)
    )
    ps[p_reg] = min(p_high, max(r.p.low, p_goal))
    r = domain.devices.resources[q_reg]
    qs[q_reg] = min(r.q.high, max(r.q.low, q_goal))
    goal = TubeCommand(p_mw=tuple(ps), q_mvar=tuple(qs))
    box = initial
    policies: list[BalancePolicy] = []
    tubes = [box]
    p_bounds: list[PowerInterval | None] = []
    q_bounds: list[PowerInterval | None] = []
    digest = sha256()
    storage = next(
        i for i, r in enumerate(domain.devices.resources) if r.kind == "storage"
    )
    for k in range(start, end):
        model = device_model_at(domain, k)
        total_p, total_q = total_load(domain, k, False), total_load(domain, k, True)
        if dynamic:
            minute_goal, _, _ = tracking_goals(
                domain, k, min(end, k + 1), nominal, (p_reg, q_reg)
            )
            minute_goal = terminal_task_allocation(
                domain, initial, start, end, target[0], minute_goal, p_reg
            )
            reference_step = max(k, deadline - 1)
            rp, rq = (
                total_load(domain, reference_step, False),
                total_load(domain, reference_step, True),
            )
            goal = allocate_goal(
                future_model(domain, k, end),
                minute_goal,
                (
                    (rp.low + rp.high) / 2 - target[0] + 0.025,
                    (rq.low + rq.high) / 2 - target[1] + 0.04,
                ),
                (p_reg, q_reg),
                shared,
            )
        next_model = (
            future_model(domain, k + 1, end)
            if dynamic
            else device_model_at(domain, k + 1)
        )
        if k + 1 < deadline:
            state = _point(box)
            ps = list(goal.p_mw)
            # Reserve enough energy for load-error compensation in the hold period.
            reserve = (
                sum(
                    (
                        total_load(domain, j, False).high
                        - total_load(domain, j, False).low
                    )
                    / 2
                    for j in range(max(k + 1, deadline - 1), end)
                )
                / 60
                / model.eta_discharge
                + domain.research.storage_tracking_reserve_mwh
            )
            high = max(
                0,
                (box.energy_mwh.low - model.energy_mwh.low - reserve)
                * 60
                * model.eta_discharge,
            )
            low = -max(
                0,
                (model.energy_mwh.high - box.energy_mwh.high - reserve)
                * 60
                / model.eta_charge,
            )
            ps[storage] = min(high, max(low, ps[storage]))
            base = approach_backup_command(
                model,
                state,
                goal.model_copy(update={"p_mw": tuple(ps)}),
                next_model=next_model,
            )
            policy = BalancePolicy(base=base)
            transition = certify_balance_policy(model, box, policy, total_p, total_q)
            ep, eq = None, None
        else:
            # Continue the certified P/Q allocation toward the new economic
            # goal even when a retained PCC target already passed its deadline.
            # Freezing the previous allocation would force PCS energy drift
            # despite a feasible reallocation at the identical PCC target.
            state = _point(box)
            base = approach_backup_command(model, state, goal, next_model=next_model)
            pw, qw = participation(model, base, (p_reg, q_reg), shared)
            offsets = (0.025, 0.04)
            for _ in range(5):
                policy = BalancePolicy(
                    base=base,
                    p_participation=pw,
                    q_participation=qw,
                    net_p_mw=target[0] - offsets[0],
                    net_q_mvar=target[1] - offsets[1],
                )
                assert policy.net_p_mw is not None and policy.net_q_mvar is not None
                transition = certify_balance_policy(
                    model, box, policy, total_p, total_q
                )
                ac = unified_network_box(
                    domain, transition.endpoint, forecast_at(domain, k)
                )
                p_resolution = transition.balance_p_error_mw
                net_p = PowerInterval(
                    low=lower(F(policy.net_p_mw) - F(p_resolution)),
                    high=upper(F(policy.net_p_mw) + F(p_resolution)),
                )
                net_q = PowerInterval(
                    low=lower(
                        F(policy.net_q_mvar) - F(transition.balance_q_error_mvar)
                    ),
                    high=upper(
                        F(policy.net_q_mvar) + F(transition.balance_q_error_mvar)
                    ),
                )
                ac = refine_ac_balance_certificate(domain.network, ac, net_p, net_q)
                if not ac.certified:
                    raise ValueError("tracking endpoint AC certificate: " + ac.reason)
                lp, lq = certified_ac_loss_bounds(domain.network, ac)
                ep = PowerInterval(
                    low=lower(F(policy.net_p_mw) + F(lp.low) - F(p_resolution)),
                    high=upper(F(policy.net_p_mw) + F(lp.high) + F(p_resolution)),
                )
                eq = PowerInterval(
                    low=lower(
                        F(policy.net_q_mvar)
                        + F(lq.low)
                        - F(transition.balance_q_error_mvar)
                    ),
                    high=upper(
                        F(policy.net_q_mvar)
                        + F(lq.high)
                        + F(transition.balance_q_error_mvar)
                    ),
                )
                if (
                    max(abs(ep.low - target[0]), abs(ep.high - target[0]))
                    <= domain.p_band_mw
                    and max(abs(eq.low - target[1]), abs(eq.high - target[1]))
                    <= domain.q_band_mvar
                ):
                    break
                offsets = ((lp.low + lp.high) / 2, (lq.low + lq.high) / 2)
            else:
                raise ValueError(
                    "loss enclosure cannot certify the requested tracking bands"
                )
        validate_device_box(device_model_at(domain, k + 1), transition.endpoint)
        network = unified_network_box(
            domain, transition.path, forecast_at(domain, k), reusable=True
        )
        if policy.net_p_mw is not None:
            assert policy.net_p_mw is not None and policy.net_q_mvar is not None
            p_resolution = transition.balance_p_error_mw
            # Monotone non-regulator P paths have drift bounded by the sum of
            # their enclosed endpoint movements. Q devices share alpha_q.
            p_drift = sum(
                (
                    max(abs(F(a.low) - F(b.high)), abs(F(a.high) - F(b.low)))
                    for i, (a, b) in enumerate(
                        zip(box.p_mw, transition.endpoint.p_mw, strict=True)
                    )
                    if i != p_reg or shared
                ),
                F(0),
            )
            q_drift = len(model.resources) * F(transition.balance_q_error_mvar)
            source_p = (
                F(total_p.low) - sum((F(x.high) for x in box.p_mw), F(0)),
                F(total_p.high) - sum((F(x.low) for x in box.p_mw), F(0)),
            )
            source_q = (
                F(total_q.low) - sum((F(x.high) for x in box.q_mvar), F(0)),
                F(total_q.high) - sum((F(x.low) for x in box.q_mvar), F(0)),
            )
            np = PowerInterval(
                low=lower(
                    min(source_p[0], F(policy.net_p_mw) - F(p_resolution)) - p_drift
                ),
                high=upper(
                    max(source_p[1], F(policy.net_p_mw) + F(p_resolution)) + p_drift
                ),
            )
            nq = PowerInterval(
                low=lower(min(source_q[0], F(policy.net_q_mvar)) - q_drift),
                high=upper(max(source_q[1], F(policy.net_q_mvar)) + q_drift),
            )
            network = refine_ac_balance_certificate(domain.network, network, np, nq)
        if not network.certified:
            raise ValueError("tracking policy path AC certificate: " + network.reason)
        digest.update(network.model_dump_json().encode())
        policies.append(policy)
        box = transition.endpoint
        tubes.append(box)
        p_bounds.append(ep)
        q_bounds.append(eq)
    tail = certify_unified_plan(domain, box, end, require_task=require_task)
    digest.update(tail.network_proof_digest.encode())
    tracking = TargetTrackingCertificate(
        target_p_mw=target[0],
        target_q_mvar=target[1],
        deadline_step=deadline,
        guaranteed_until_step=end,
        nominal=nominal,
        construction=construction,
        policies=tuple(policies),
        tubes=tuple(tubes),
        pcc_p_mw=tuple(p_bounds),
        pcc_q_mvar=tuple(q_bounds),
    )
    return UnifiedPlanCertificate(
        start_step=start,
        initial=initial,
        commands=tuple(p.base for p in policies) + tail.commands,
        terminal=tail.terminal,
        network_proof_digest=digest.hexdigest(),
        tracking=tracking,
        task_continuation=tail.task_continuation,
        task_reason=tail.task_reason,
    )


@lru_cache(maxsize=128)
def certify_tracking_target(
    domain: UnifiedDomain,
    initial: TubeBox,
    start: int,
    end: int,
    deadline: int,
    target: tuple[float, float],
    nominal: TubeCommand,
    *,
    require_task: bool = True,
) -> UnifiedPlanCertificate:
    """Keep the target fixed; rank certified allocations by intervention then cost.

    The finite search has a deterministic, recorded budget. Exhaustion is not
    a physical infeasibility proof. Every option goes through the same full
    device, next-minute capability, AC, tracking and hard-task certification.
    """
    _, p, q = tracking_goals(domain, start, end, nominal)
    trials: list[AllocationTrial] = []
    candidates: list[tuple[float, float, int, UnifiedPlanCertificate]] = []
    for index, _ in enumerate(allocation_options(domain, (p, q))):
        try:
            plan = _certify_strategy(
                domain,
                initial,
                start,
                end,
                deadline,
                target,
                nominal,
                require_task=require_task,
                construction=index,
            )
            intervention = intervention_score(domain, plan, nominal)
            point_initial = all(
                x.low == x.high
                for x in (*initial.p_mw, *initial.q_mvar, initial.energy_mwh)
            )
            cost = admitted_policy_cost(domain, plan) if point_initial else None
        except ValueError as exc:
            trials.append(
                AllocationTrial(
                    construction=index, status="construction_unproven", reason=str(exc)
                )
            )
        else:
            trials.append(
                AllocationTrial(
                    construction=index,
                    status="certified",
                    intervention=intervention,
                    economic_cost_cny=cost,
                )
            )
            candidates.append((intervention, cost or 0.0, index, plan))
            if len(candidates) >= domain.research.certified_economic_candidates:
                break
    if not candidates:
        reasons = "; ".join(f"{t.construction}: {t.reason}" for t in trials)
        raise ValueError(
            f"allocation search construction_unproven ({len(trials)} attempts): {reasons}"
        )
    plan = min(candidates, key=lambda x: x[:3])[3]
    assert plan.tracking is not None
    return plan.model_copy(
        update={
            "tracking": plan.tracking.model_copy(
                update={"allocation_trials": tuple(trials)}
            )
        }
    )

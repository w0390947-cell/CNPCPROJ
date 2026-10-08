"""Deterministic allocation candidates; no candidate is an execution permission."""

from oilfield_energy.modules.control.api import (
    balance_policy_command,
    execute_tube_command,
)
from oilfield_energy.modules.control.contracts import (
    TubeBox,
    TubeCommand,
    TubeDeviceModel,
    TubeState,
)

from .contracts_unified import UnifiedDomain, UnifiedPlanCertificate
from .network import device_model_at, forecast_at


def allocation_options(
    domain: UnifiedDomain, preferred: tuple[int, int]
) -> tuple[tuple[int, int, bool, bool], ...]:
    """Dynamic local, shared, constant-envelope, then alternate regulators."""
    p, q = preferred
    options = [(p, q, False, True), (p, q, True, True), (p, q, False, False)]
    resources = domain.devices.resources
    for i, r in enumerate(resources):
        if r.p.high <= r.p.low:
            continue
        for j, s in enumerate(resources):
            if s.q.high > s.q.low and (i, j) != preferred:
                options.append((i, j, False, True))
    return tuple(options[: domain.research.allocation_search_budget])


def _sum_projection(
    values: list[float], lows: list[float], highs: list[float], total: float
) -> list[float]:
    """Least-squares allocation to a box and a feasible aggregate setpoint."""
    total = min(sum(highs), max(sum(lows), total))
    left = min(lo - x for lo, x in zip(lows, values, strict=True))
    right = max(hi - x for hi, x in zip(highs, values, strict=True))
    for _ in range(60):
        shift = (left + right) / 2
        if (
            sum(
                min(hi, max(lo, x + shift))
                for x, lo, hi in zip(values, lows, highs, strict=True)
            )
            < total
        ):
            left = shift
        else:
            right = shift
    return [
        min(hi, max(lo, x + (left + right) / 2))
        for x, lo, hi in zip(values, lows, highs, strict=True)
    ]


def allocate_goal(
    model: TubeDeviceModel,
    goal: TubeCommand,
    totals: tuple[float, float],
    preferred: tuple[int, int],
    shared: bool,
) -> TubeCommand:
    axes: list[tuple[float, ...]] = []
    for reactive, values, total, regulator in (
        (False, goal.p_mw, totals[0], preferred[0]),
        (True, goal.q_mvar, totals[1], preferred[1]),
    ):
        bounds = [r.q if reactive else r.p for r in model.resources]
        lows, highs = [x.low for x in bounds], [x.high for x in bounds]
        # Q/P coupling is applied before the final joint command projection.
        if reactive:
            for i, r in enumerate(model.resources):
                if r.q_over_p is not None:
                    cap = max(0.0, r.q_over_p * axes[0][i] - 1e-6)
                    lows[i], highs[i] = max(lows[i], -cap), min(highs[i], cap)
        current = [
            min(hi, max(lo, x)) for x, lo, hi in zip(values, lows, highs, strict=True)
        ]
        if not shared:
            current[regulator] = min(
                highs[regulator],
                max(
                    lows[regulator],
                    total - sum(x for i, x in enumerate(current) if i != regulator),
                ),
            )
        if shared or abs(sum(current) - total) > 1e-10:
            current = _sum_projection(current, lows, highs, total)
        axes.append(tuple(current))
    return TubeCommand(p_mw=axes[0], q_mvar=axes[1])


def participation(
    model: TubeDeviceModel, base: TubeCommand, preferred: tuple[int, int], shared: bool
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    axes: list[tuple[float, ...]] = []
    for reactive, values, regulator in (
        (False, base.p_mw, preferred[0]),
        (True, base.q_mvar, preferred[1]),
    ):
        scores: list[float] = []
        for r, x in zip(model.resources, values, strict=True):
            bounds = r.q if reactive else r.p
            margin = max(0.0, min(bounds.high - x, x - bounds.low))
            if reactive and r.q_over_p is not None:
                margin = min(
                    margin, max(0.0, r.q_over_p * base.p_mw[len(scores)] - abs(x))
                )
            scores.append(margin if shared else 0.0)
        if not any(scores):
            scores[regulator] = 1.0
        axes.append(tuple(scores))
    return axes[0], axes[1]


def future_model(domain: UnifiedDomain, step: int, end: int) -> TubeDeviceModel:
    """Backward braking envelope for declining renewable availability.

    This is only a P construction bound; coupled P/Q and the complete safe tail
    still have to be certified. It does not label a rejected state unreachable.
    """
    model = device_model_at(domain, step)
    resources = list(model.resources)
    for i, r in enumerate(resources):
        if r.kind not in ("wind", "pv"):
            continue
        bound = device_model_at(domain, end).resources[i].p.high
        for k in range(end - 1, step - 1, -1):
            cap = device_model_at(domain, k).resources[i].p.high
            decay = (
                max(bound, model.settling_resolution) / (1 - r.alpha_p)
                if r.alpha_p < 1
                else cap
            )
            bound = min(cap, bound + 0.98 * r.ramp_p_mw, decay)
        resources[i] = r.model_copy(
            update={"p": r.p.model_copy(update={"high": bound})}
        )
    return model.model_copy(update={"resources": tuple(resources)})


def intervention_score(
    domain: UnifiedDomain, plan: UnifiedPlanCertificate, nominal: TubeCommand
) -> float:
    """Squared command intervention on each resource's declared MVA base."""
    assert plan.tracking is not None
    box: TubeBox = plan.initial
    state = TubeState(
        p_mw=tuple((x.low + x.high) / 2 for x in box.p_mw),
        q_mvar=tuple((x.low + x.high) / 2 for x in box.q_mvar),
        energy_mwh=(box.energy_mwh.low + box.energy_mwh.high) / 2,
    )
    score = 0.0
    for offset, policy in enumerate(plan.tracking.policies):
        k = plan.start_step + offset
        forecast = forecast_at(domain, k)
        model = device_model_at(domain, k)
        p = sum((b.p_mw.low + b.p_mw.high) / 2 for b in forecast.loads)
        q = sum((b.q_mvar.low + b.q_mvar.high) / 2 for b in forecast.loads)
        command = balance_policy_command(model, state, policy, p, q)
        score += sum(
            ((u - v) / r.s_mva) ** 2 + ((a - b) / r.s_mva) ** 2
            for r, u, v, a, b in zip(
                model.resources,
                command.p_mw,
                nominal.p_mw,
                command.q_mvar,
                nominal.q_mvar,
                strict=True,
            )
        )
        state = execute_tube_command(model, state, command)
    return score / len(plan.tracking.policies)

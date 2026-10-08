"""Shared-residual feedback with correlated device and aggregate enclosures."""

from fractions import Fraction as F
from math import ulp

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.kernel.intervals import Interval

from .application_tube import (
    certify_device_transition,
    device_response,
    response_image,
    tube_hull,
    validate_device_box,
    validate_resource_box,
)
from .contracts_policy import BalancePolicy, PolicyTransition
from .contracts_tube import TubeBox, TubeCommand, TubeDeviceModel, TubeState


def _weights(policy: BalancePolicy, reactive: bool) -> tuple[F, ...]:
    scores = tuple(F(x) for x in policy.weights(reactive))
    total = sum(scores, F(0))
    return tuple(x / total if total else F(0) for x in scores)


def balance_policy_command(
    model: TubeDeviceModel,
    state: TubeState,
    policy: BalancePolicy,
    total_p_mw: float,
    total_q_mvar: float,
) -> TubeCommand:
    """Allocate one measured residual; no participant clips or overrides it.

    Regulating devices use unsnapped affine reference endpoints. Nonregulating
    devices use their exact settling response. The certificate includes the
    subsequent settling/float error of every participant, including the PCS.
    """
    axes: list[tuple[float, ...]] = []
    for reactive, states, base, demand, net in (
        (False, state.p_mw, policy.base.p_mw, total_p_mw, policy.net_p_mw),
        (True, state.q_mvar, policy.base.q_mvar, total_q_mvar, policy.net_q_mvar),
    ):
        weights = _weights(policy, reactive)
        alphas = tuple(
            model.alpha_q if reactive else r.alpha_p for r in model.resources
        )
        endpoints = tuple(
            (1 - F(a)) * F(x) + F(a) * F(u)
            if w
            else device_response(F(x), u, a, model.settling_resolution)
            for x, u, a, w in zip(states, base, alphas, weights, strict=True)
        )
        residual = (
            F(demand) - F(net) - sum(endpoints, F(0)) if net is not None else F(0)
        )
        axes.append(
            tuple(
                float(F(u) + w * residual / F(a)) if w else u
                for u, w, a in zip(base, weights, alphas, strict=True)
            )
        )
    return TubeCommand(p_mw=axes[0], q_mvar=axes[1])


def _axis(
    model: TubeDeviceModel,
    source: tuple[Interval, ...],
    base: tuple[float, ...],
    weights: tuple[F, ...],
    alphas: tuple[float, ...],
    demand: Interval,
    net: float | None,
) -> tuple[tuple[Interval, ...], tuple[Interval, ...], tuple[F, ...], float]:
    refs = tuple(
        Interval(
            low=lower((1 - F(a)) * F(x.low) + F(a) * F(u)),
            high=upper((1 - F(a)) * F(x.high) + F(a) * F(u)),
        )
        if w
        else response_image(x, u, a, model.settling_resolution)
        for x, u, a, w in zip(source, base, alphas, weights, strict=True)
    )
    commands: list[Interval] = []
    endpoints: list[Interval] = []
    movements: list[F] = []
    total_error = F(0)
    for i, (x, u, a_float, w) in enumerate(
        zip(source, base, alphas, weights, strict=True)
    ):
        a = F(a_float)
        if not w:
            command, endpoint = Interval(low=u, high=u), refs[i]
            movement = max(
                abs(F(endpoint.high) - F(x.low)),
                abs(F(x.high) - F(endpoint.low)),
                a * max(abs(F(u) - F(x.low)), abs(F(u) - F(x.high))),
            )
        else:
            assert net is not None
            other_lo = sum((F(v.low) for j, v in enumerate(refs) if j != i), F(0))
            other_hi = sum((F(v.high) for j, v in enumerate(refs) if j != i), F(0))
            residual_lo = F(demand.low) - F(net) - other_hi - F(refs[i].high)
            residual_hi = F(demand.high) - F(net) - other_lo - F(refs[i].low)
            command = Interval(
                low=lower(F(u) + w * residual_lo / a),
                high=upper(F(u) + w * residual_hi / a),
            )
            # Float input sum, command conversion, endpoint rounding and snapping.
            error = F(model.settling_resolution) if a != 1 else F(0)
            error += 8 * (
                F(ulp(max(abs(demand.low), abs(demand.high))))
                + F(ulp(max(abs(command.low), abs(command.high))))
                + sum((F(ulp(max(abs(v.low), abs(v.high)))) for v in refs), F(0))
            )
            endpoint = Interval(
                low=lower(
                    (1 - w) * F(refs[i].low)
                    + w * (F(demand.low) - F(net) - other_hi)
                    - error
                ),
                high=upper(
                    (1 - w) * F(refs[i].high)
                    + w * (F(demand.high) - F(net) - other_lo)
                    + error
                ),
            )
            # u-x is monotone decreasing in this resource's own source state;
            # preserve that correlation instead of crossing independent boxes.
            delta_lo = F(u) + w * residual_lo / a - F(x.high)
            delta_hi = F(u) + w * residual_hi / a - F(x.low)
            movement = a * max(abs(delta_lo), abs(delta_hi)) + error
            total_error += error
        commands.append(command)
        endpoints.append(endpoint)
        movements.append(movement)
    # Nonparticipants round once during execution as well.
    if net is not None:
        total_error += sum(
            (F(ulp(max(abs(v.low), abs(v.high)))) for v in endpoints), F(0)
        )
    return tuple(commands), tuple(endpoints), tuple(movements), upper(total_error)


def certify_balance_policy(
    model: TubeDeviceModel,
    source: TubeBox,
    policy: BalancePolicy,
    total_p: Interval,
    total_q: Interval,
) -> PolicyTransition:
    """Enclose every source/load pair without enumerating 4**N command corners.

    Source/command correlation is retained for response budgets and affine
    endpoint cancellation. Capability still uses rectangular P/Q enclosures;
    rejection therefore remains an unproven construction, not infeasibility.
    """
    if policy.net_p_mw is None and policy.net_q_mvar is None:
        transition = certify_device_transition(model, source, policy.base)
        return PolicyTransition(endpoint=transition.endpoint, path=transition.path)
    validate_device_box(model, source)
    if len(policy.base.p_mw) != len(model.resources):
        raise ValueError("policy resource axis differs")
    cp, ep, mp, pe = _axis(
        model,
        source.p_mw,
        policy.base.p_mw,
        _weights(policy, False),
        tuple(r.alpha_p for r in model.resources),
        total_p,
        policy.net_p_mw,
    )
    cq, eq, mq, qe = _axis(
        model,
        source.q_mvar,
        policy.base.q_mvar,
        _weights(policy, True),
        (model.alpha_q,) * len(model.resources),
        total_q,
        policy.net_q_mvar,
    )
    for r, p, q, movement in zip(model.resources, cp, cq, mp, strict=True):
        validate_resource_box(r, p, q)
        if movement > F(r.ramp_p_mw):
            raise ValueError(f"feedback P response budget: {r.resource_id}")
    if sum(mq, F(0)) > F(model.aggregate_q_ramp_mvar):
        raise ValueError("feedback Q aggregate response budget")
    storage = next(i for i, r in enumerate(model.resources) if r.kind == "storage")

    def energy_delta(power: float) -> F:
        p = F(power)
        return (
            (-p / F(model.eta_discharge) if p >= 0 else -p * F(model.eta_charge))
            * F(model.step_minutes)
            / 60
        )

    endpoint = TubeBox(
        p_mw=ep,
        q_mvar=eq,
        energy_mwh=Interval(
            low=lower(F(source.energy_mwh.low) + energy_delta(cp[storage].high)),
            high=upper(F(source.energy_mwh.high) + energy_delta(cp[storage].low)),
        ),
    )
    validate_device_box(model, endpoint)
    path = tube_hull(source, endpoint)
    for r, p, q in zip(model.resources, path.p_mw, path.q_mvar, strict=True):
        if r.alpha_p != model.alpha_q:
            validate_resource_box(r, p, q)
    return PolicyTransition(
        endpoint=endpoint, path=path, balance_p_error_mw=pe, balance_q_error_mvar=qe
    )

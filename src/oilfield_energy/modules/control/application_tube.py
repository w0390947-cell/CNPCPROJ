"""Exact affine reachable boxes inside the ordinary, unsaturated response domain."""

from fractions import Fraction as F

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.kernel.intervals import Interval

from .contracts_tube import (
    TubeBox,
    TubeCommand,
    TubeDeviceModel,
    TubeResource,
    TubeState,
    TubeTransition,
)


def point_box(state: TubeState) -> TubeBox:
    return TubeBox(
        p_mw=tuple(Interval(low=x, high=x) for x in state.p_mw),
        q_mvar=tuple(Interval(low=x, high=x) for x in state.q_mvar),
        energy_mwh=Interval(low=state.energy_mwh, high=state.energy_mwh),
    )


def tube_contains(box: TubeBox, state: TubeState) -> bool:
    return (
        len(box.p_mw) == len(state.p_mw)
        and len(box.q_mvar) == len(state.q_mvar)
        and all(x.contains(v) for x, v in zip(box.p_mw, state.p_mw, strict=True))
        and all(x.contains(v) for x, v in zip(box.q_mvar, state.q_mvar, strict=True))
        and box.energy_mwh.contains(state.energy_mwh)
    )


def tube_hull(first: TubeBox, second: TubeBox) -> TubeBox:
    def hull(a: Interval, b: Interval) -> Interval:
        return Interval(low=min(a.low, b.low), high=max(a.high, b.high))

    return TubeBox(
        p_mw=tuple(hull(a, b) for a, b in zip(first.p_mw, second.p_mw, strict=True)),
        q_mvar=tuple(hull(a, b) for a, b in zip(first.q_mvar, second.q_mvar, strict=True)),
        energy_mwh=hull(first.energy_mwh, second.energy_mwh),
    )


def validate_resource_box(r: TubeResource, p: Interval, q: Interval) -> None:
    pm, qm = max(abs(F(p.low)), abs(F(p.high))), max(abs(F(q.low)), abs(F(q.high)))
    if (
        p.low < r.p.low
        or p.high > r.p.high
        or q.low < r.q.low
        or q.high > r.q.high
        or pm**2 + qm**2 > F(r.s_mva) ** 2
        or (r.q_over_p is not None and qm > F(r.q_over_p) * max(F(0), F(p.low)))
    ):
        raise ValueError(f"device path capability: {r.resource_id}")


def validate_device_box(model: TubeDeviceModel, box: TubeBox) -> None:
    if len(box.p_mw) != len(model.resources) or len(box.q_mvar) != len(model.resources):
        raise ValueError("tube resource axis differs")
    if box.energy_mwh.low < model.energy_mwh.low or box.energy_mwh.high > model.energy_mwh.high:
        raise ValueError("storage energy envelope")
    for r, p, q in zip(model.resources, box.p_mw, box.q_mvar, strict=True):
        validate_resource_box(r, p, q)


def device_response(value: F, target: float, alpha: float, resolution: float) -> F:
    result = (1 - F(alpha)) * value + F(alpha) * F(target)
    return F(target) if abs(result - F(target)) <= F(resolution) else result


def response_image(interval: Interval, target: float, alpha: float, resolution: float) -> Interval:
    a, u = F(alpha), F(target)
    return Interval(
        low=lower(device_response(F(interval.low), float(u), float(a), resolution)),
        high=upper(device_response(F(interval.high), float(u), float(a), resolution)),
    )


def certify_device_transition(
    model: TubeDeviceModel, box: TubeBox, command: TubeCommand
) -> TubeTransition:
    """Every source state uses one cycle; first-step budget excludes saturation."""
    if model.version != "unified-device-v1":
        raise ValueError("single ZOH certificate cannot certify the cluster energy model")
    validate_device_box(model, box)
    if len(command.p_mw) != len(model.resources) or len(command.q_mvar) != len(model.resources):
        raise ValueError("command resource axis differs")
    target = TubeBox(
        p_mw=tuple(Interval(low=x, high=x) for x in command.p_mw),
        q_mvar=tuple(Interval(low=x, high=x) for x in command.q_mvar),
        energy_mwh=box.energy_mwh,
    )
    validate_device_box(model, target)
    for r, p, u in zip(model.resources, box.p_mw, command.p_mw, strict=True):
        if F(r.alpha_p) * max(abs(F(u) - F(p.low)), abs(F(u) - F(p.high))) > F(r.ramp_p_mw):
            raise ValueError(f"ordinary P response would saturate: {r.resource_id}")
    q_move = sum(
        (
            F(model.alpha_q) * max(abs(F(u) - F(q.low)), abs(F(u) - F(q.high)))
            for q, u in zip(box.q_mvar, command.q_mvar, strict=True)
        ),
        F(0),
    )
    if q_move > F(model.aggregate_q_ramp_mvar):
        raise ValueError("ordinary Q aggregate response would saturate")
    ps = tuple(
        response_image(p, u, r.alpha_p, model.settling_resolution)
        for r, p, u in zip(model.resources, box.p_mw, command.p_mw, strict=True)
    )
    qs = tuple(
        response_image(q, u, model.alpha_q, model.settling_resolution)
        for q, u in zip(box.q_mvar, command.q_mvar, strict=True)
    )
    # Include endpoint rounding in the same budget; no extra numerical ramp.
    if any(
        max(abs(F(end.high) - F(start.low)), abs(F(start.high) - F(end.low))) > F(r.ramp_p_mw)
        for r, start, end in zip(model.resources, box.p_mw, ps, strict=True)
    ):
        raise ValueError("enclosed P movement exceeds the cycle budget")
    if sum(
        (
            max(abs(F(end.high) - F(start.low)), abs(F(start.high) - F(end.low)))
            for start, end in zip(box.q_mvar, qs, strict=True)
        ),
        F(0),
    ) > F(model.aggregate_q_ramp_mvar):
        raise ValueError("enclosed Q movement exceeds the cycle budget")
    storage = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    # PCS sets the accepted power once and holds it over this execution interval.
    power = F(command.p_mw[storage])
    delta = (
        (-power / F(model.eta_discharge) if power >= 0 else -power * F(model.eta_charge))
        * F(model.step_minutes)
        / 60
    )
    energy = Interval(
        low=lower(F(box.energy_mwh.low) + delta),
        high=upper(F(box.energy_mwh.high) + delta),
    )
    end = TubeBox(p_mw=ps, q_mvar=qs, energy_mwh=energy)
    path = tube_hull(box, end)
    validate_device_box(model, end)
    for r, p, q in zip(model.resources, path.p_mw, path.q_mvar, strict=True):
        # Matched first-order P/Q responses traverse the convex segment between
        # each admissible initial point and command. A rectangular path would
        # discard this correlation and falsely reject Q/P-limited wind curtailment.
        # Unmatched responses retain the conservative rectangular capability test.
        if r.alpha_p != model.alpha_q:
            validate_resource_box(r, p, q)
    return TubeTransition(initial=box, command=command, endpoint=end, path=path)


def execute_tube_command(
    model: TubeDeviceModel, state: TubeState, command: TubeCommand
) -> TubeState:
    """Ordinary model execution, rounded once; no clipping or protection override."""
    certify_device_transition(model, point_box(state), command)
    ps = tuple(
        float(device_response(F(p), u, r.alpha_p, model.settling_resolution))
        for r, p, u in zip(model.resources, state.p_mw, command.p_mw, strict=True)
    )
    qs = tuple(
        float(device_response(F(q), u, model.alpha_q, model.settling_resolution))
        for q, u in zip(state.q_mvar, command.q_mvar, strict=True)
    )
    i = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    p = F(command.p_mw[i])
    delta = (
        (-p / F(model.eta_discharge) if p >= 0 else -p * F(model.eta_charge))
        * F(model.step_minutes)
        / 60
    )
    return TubeState(p_mw=ps, q_mvar=qs, energy_mwh=float(F(state.energy_mwh) + delta))

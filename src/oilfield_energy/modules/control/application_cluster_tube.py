"""Lagged control nodes with linear P/Q paths and exact PCS energy, model v1.

All channels interpolate linearly, so node movement also bounds continuous
slew (the shared Q budget bounds the sum of absolute Q derivatives).
The response is inside the closed endpoint hull. Exact rational integration
includes charge/discharge reversal; no saturation or hidden safety override.
"""

# pyright: strict
from fractions import Fraction as F
from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.kernel.intervals import Interval
from .contracts_tube import (
    TubeDeviceModel,
    TubeBox,
    TubeCommand,
    TubeState,
    TubeTransition,
)
from .application_tube import (
    validate_device_box,
    response_image,
    device_response,
    point_box,
    tube_hull,
)


def _energy(a: F, b: F, model: TubeDeviceModel) -> F:
    """Integral of efficiency-adjusted power along the linear endpoint segment."""
    h = F(model.step_minutes) / 60
    if a >= 0 and b >= 0:
        return -(a + b) * h / (2 * F(model.eta_discharge))
    if a <= 0 and b <= 0:
        return -(a + b) * h * F(model.eta_charge) / 2
    positive, negative = max(a, b), -min(a, b)
    return (
        h
        * (negative**2 * F(model.eta_charge) - positive**2 / F(model.eta_discharge))
        / (2 * (positive + negative))
    )


def certify_cluster_device_transition(
    model: TubeDeviceModel, box: TubeBox, command: TubeCommand
) -> TubeTransition:
    if model.version != "cluster-device-v1":
        raise ValueError("cluster proof requires its declared response/energy model")
    validate_device_box(model, box)
    if len(command.p_mw) != len(model.resources) or len(command.q_mvar) != len(
        model.resources
    ):
        raise ValueError("command identity axis differs")
    target = TubeBox(
        p_mw=tuple(Interval(low=x, high=x) for x in command.p_mw),
        q_mvar=tuple(Interval(low=x, high=x) for x in command.q_mvar),
        energy_mwh=box.energy_mwh,
    )
    validate_device_box(model, target)
    ps = tuple(
        response_image(
            p,
            u,
            r.alpha_p,
            min(1e-12, model.settling_resolution)
            if r.kind == "storage"
            else model.settling_resolution,
        )
        for r, p, u in zip(model.resources, box.p_mw, command.p_mw, strict=True)
    )
    qs = tuple(
        response_image(q, u, model.alpha_q, model.settling_resolution)
        for q, u in zip(box.q_mvar, command.q_mvar, strict=True)
    )
    for r, p, y in zip(model.resources, box.p_mw, ps, strict=True):
        if max(abs(F(y.high) - F(p.low)), abs(F(p.high) - F(y.low))) > F(r.ramp_p_mw):
            raise ValueError(
                "cluster P movement exceeds cycle budget: " + r.resource_id
            )
    if sum(
        (
            max(abs(F(y.high) - F(q.low)), abs(F(q.high) - F(y.low)))
            for q, y in zip(box.q_mvar, qs, strict=True)
        ),
        F(0),
    ) > F(model.aggregate_q_ramp_mvar):
        raise ValueError("cluster Q movement exceeds aggregate cycle budget")
    i = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    # Energy is monotone decreasing in each power endpoint. Independent corners
    # conservatively enclose correlated lag responses as well.
    delta_low = _energy(F(box.p_mw[i].high), F(ps[i].high), model)
    delta_high = _energy(F(box.p_mw[i].low), F(ps[i].low), model)
    energy = Interval(
        low=lower(F(box.energy_mwh.low) + delta_low),
        high=upper(F(box.energy_mwh.high) + delta_high),
    )
    endpoint = TubeBox(p_mw=ps, q_mvar=qs, energy_mwh=energy)
    path = tube_hull(box, endpoint)
    # A reversing PCS can reach an energy extremum inside the minute.
    discharge = (
        max(F(0), F(box.p_mw[i].high), F(ps[i].high))
        * F(model.step_minutes)
        / (60 * F(model.eta_discharge))
    )
    charge = (
        max(F(0), -F(box.p_mw[i].low), -F(ps[i].low))
        * F(model.step_minutes)
        * F(model.eta_charge)
        / 60
    )
    path = path.model_copy(
        update={
            "energy_mwh": Interval(
                low=lower(F(box.energy_mwh.low) - discharge),
                high=upper(F(box.energy_mwh.high) + charge),
            )
        }
    )
    validate_device_box(model, endpoint)
    validate_device_box(model, path)
    return TubeTransition(initial=box, command=command, endpoint=endpoint, path=path)


def execute_cluster_device_command(
    model: TubeDeviceModel, state: TubeState, command: TubeCommand
) -> TubeState:
    certify_cluster_device_transition(model, point_box(state), command)
    ps = tuple(
        float(
            device_response(
                F(p),
                u,
                r.alpha_p,
                min(1e-12, model.settling_resolution)
                if r.kind == "storage"
                else model.settling_resolution,
            )
        )
        for r, p, u in zip(model.resources, state.p_mw, command.p_mw, strict=True)
    )
    qs = tuple(
        float(device_response(F(q), u, model.alpha_q, model.settling_resolution))
        for q, u in zip(state.q_mvar, command.q_mvar, strict=True)
    )
    i = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    return TubeState(
        p_mw=ps,
        q_mvar=qs,
        energy_mwh=float(
            F(state.energy_mwh) + _energy(F(state.p_mw[i]), F(ps[i]), model)
        ),
    )

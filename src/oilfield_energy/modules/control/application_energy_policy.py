"""Monotone energy feedback; see modeling/Single_Task_Continuation.md."""

from fractions import Fraction as F
from math import ulp

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.kernel.intervals import Interval

from .application_tube import certify_device_transition, tube_hull, validate_device_box
from .contracts_energy_policy import EnergyReturnPolicy
from .contracts_policy import PolicyTransition
from .contracts_tube import TubeBox, TubeCommand, TubeDeviceModel, TubeState, TubeTransition


def energy_return_command(
    model: TubeDeviceModel, state: TubeState, policy: EnergyReturnPolicy
) -> TubeCommand:
    i = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    error = F(state.energy_mwh) - F(policy.target_mwh)
    hours = F(model.step_minutes) / 60
    ideal = (
        error * F(model.eta_discharge) / hours
        if error >= 0
        else error / F(model.eta_charge) / hours
    )
    power = min(F(policy.discharge_limit_mw), max(-F(policy.charge_limit_mw), ideal))
    ps = list(policy.base.p_mw)
    ps[i] = float(power)
    return policy.base.model_copy(update={"p_mw": tuple(ps)})


def certify_energy_return(
    model: TubeDeviceModel, source: TubeBox, policy: EnergyReturnPolicy
) -> PolicyTransition:
    """Enclose the feedback graph, including both saturation regions.

    P command and exact remaining energy are monotone in initial energy.
    Capability/ramp checks retain the complete P/Q source box at both command
    extremes. Energy is paired with its own command. Interior floating command
    rounding is bounded separately; the executor still uses the same PCS.
    """
    validate_device_box(model, source)
    transitions: list[TubeTransition] = []
    for energy in (source.energy_mwh.low, source.energy_mwh.high):
        state = TubeState(
            p_mw=tuple(x.low for x in source.p_mw),
            q_mvar=tuple(x.low for x in source.q_mvar),
            energy_mwh=energy,
        )
        command = energy_return_command(model, state, policy)
        transitions.append(
            certify_device_transition(
                model,
                source.model_copy(
                    update={"energy_mwh": Interval(low=energy, high=energy)}
                ),
                command,
            )
        )
    endpoint = tube_hull(transitions[0].endpoint, transitions[1].endpoint)
    cap = max(policy.charge_limit_mw, policy.discharge_limit_mw)
    error = F(ulp(cap)) * F(model.step_minutes) / 60 / F(model.eta_discharge)
    target = F(policy.target_mwh)
    lo, hi = F(source.energy_mwh.low), F(source.energy_mwh.high)
    hours = F(model.step_minutes) / 60
    linear_low = target - F(policy.charge_limit_mw) * F(model.eta_charge) * hours
    linear_high = target + F(policy.discharge_limit_mw) / F(model.eta_discharge) * hours
    if lo <= linear_high and hi >= linear_low:
        endpoint = endpoint.model_copy(
            update={
                "energy_mwh": Interval(
                    low=lower(min(F(endpoint.energy_mwh.low), target - error)),
                    high=upper(max(F(endpoint.energy_mwh.high), target + error)),
                )
            }
        )
    validate_device_box(model, endpoint)
    return PolicyTransition(endpoint=endpoint, path=tube_hull(source, endpoint))

"""PCS stopping candidates for a certified tail, independent of other axes."""

from fractions import Fraction as F
from math import ceil

from oilfield_energy.kernel.enclosures import lower, sqrt_bounds, upper

from .application_safe_command import approach_device_command
from .contracts_tube import TubeBox, TubeCommand, TubeDeviceModel, TubeState


def _stopping_energy(model: TubeDeviceModel, power: float, ramp: float) -> F:
    """Necessary energy including this cycle and fastest subsequent ramp to zero."""
    p, r = abs(F(power)), F(ramp)
    if not p:
        return F(0)
    if not r:
        raise ValueError("PCS cannot reach zero with a zero ramp budget")
    count = ceil(p / r)
    total = count * p - r * count * (count - 1) / 2
    hours = F(model.step_minutes) / 60
    return (
        total
        * hours
        * (1 / F(model.eta_discharge) if power > 0 else F(model.eta_charge))
    )


def _independent_stop(
    model: TubeDeviceModel,
    source: TubeBox,
    command: TubeCommand,
    next_model: TubeDeviceModel | None,
) -> TubeCommand:
    i = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    resource = model.resources[i]
    previous = source.p_mw[i]
    ramp = F(resource.ramp_p_mw)
    # One constant command must satisfy the ramp at EVERY point of the source.
    lo = max(F(resource.p.low), F(previous.high) - ramp)
    hi = min(F(resource.p.high), F(previous.low) + ramp)
    if next_model is not None:
        lo = max(lo, F(next_model.resources[i].p.low))
        hi = min(hi, F(next_model.resources[i].p.high))
    q = max(
        abs(F(source.q_mvar[i].low)),
        abs(F(source.q_mvar[i].high)),
        abs(F(command.q_mvar[i])),
    )
    square = F(resource.s_mva) ** 2 - q**2
    if square < 0:
        raise ValueError("PCS source or command Q exceeds its rating")
    capacity = sqrt_bounds(square)[0]
    lo, hi = max(lo, -capacity), min(hi, capacity)
    if resource.q_over_p is not None:
        if not resource.q_over_p and q:
            raise ValueError("PCS Q/P constraint has no stopping interval")
        if resource.q_over_p:
            lo = max(lo, q / F(resource.q_over_p))
    low, high = upper(lo), lower(hi)  # Inward rounding, no numerical ramp credit.
    if low > high:
        raise ValueError("PCS source tube has no common stopping command")
    power = min(high, max(low, 0.0))
    required = _stopping_energy(model, power, resource.ramp_p_mw)
    available = (
        F(source.energy_mwh.low) - F(model.energy_mwh.low)
        if power >= 0
        else F(model.energy_mwh.high) - F(source.energy_mwh.high)
    )
    if required > available:
        raise ValueError("PCS stopping energy excludes the source tube")
    return command.model_copy(
        update={
            "p_mw": tuple(power if j == i else p for j, p in enumerate(command.p_mw))
        }
    )


def backup_command_candidates(
    model: TubeDeviceModel,
    source: TubeBox,
    terminal: TubeCommand,
    *,
    movement_fraction: float = 1.0,
    next_model: TubeDeviceModel | None = None,
    independent_pcs: bool = True,
) -> tuple[TubeCommand, ...]:
    """Prefer independent PCS recovery; every candidate still needs a full proof.

    The scaled candidate remains available for network restrictions. The caller
    may also retry the entire scaled tail if a locally valid independent action
    prevents later continuation. This API is only for a zero-PCS terminal tail;
    economic tracking with a nonzero PCS target uses the general projection.
    """
    i = next(i for i, r in enumerate(model.resources) if r.kind == "storage")
    if terminal.p_mw[i] != 0:
        raise ValueError("backup recovery requires a zero PCS terminal target")
    state = TubeState(
        p_mw=tuple((p.low + p.high) / 2 for p in source.p_mw),
        q_mvar=tuple((q.low + q.high) / 2 for q in source.q_mvar),
        energy_mwh=(source.energy_mwh.low + source.energy_mwh.high) / 2,
    )
    scaled = approach_device_command(
        model,
        state,
        terminal,
        movement_fraction=movement_fraction,
        next_model=next_model,
    )
    if not independent_pcs:
        return (scaled,)
    try:
        independent = _independent_stop(model, source, scaled, next_model)
    except ValueError:
        # A failed inner construction is not a global infeasibility verdict.
        return (scaled,)
    return (independent,) if independent == scaled else (independent, scaled)

"""Candidate P/Q reallocation at the unchanged PCC target near a hard deadline."""

from oilfield_energy.modules.control.contracts import TubeBox, TubeCommand

from .contracts_unified import UnifiedDomain
from .network import device_model_at, forecast_at


def terminal_task_allocation(
    domain: UnifiedDomain,
    source: TubeBox,
    start: int,
    end: int,
    target_p_mw: float,
    goal: TubeCommand,
    p_regulator: int,
) -> TubeCommand:
    """Generate a candidate only; complete dynamic/network certification follows.

    Adjust renewable allocation, not the accepted PCC target. This is needed
    when an old allocation would continue charging through the energy deadline.
    Resource caps and relative headroom determine allocation, not site names.
    """
    task = domain.energy_task
    model = domain.devices
    if (
        task is None
        or task.deadline_step != end
        or model.resources[p_regulator].kind != "storage"
    ):
        return goal
    hours = (end - start) * model.step_minutes / 60
    energy = (source.energy_mwh.low + source.energy_mwh.high) / 2
    error = energy - (task.energy_mwh.low + task.energy_mwh.high) / 2
    storage = model.resources[p_regulator]
    power = (
        error * model.eta_discharge / hours
        if error >= 0
        else error / (model.eta_charge * hours)
    )
    power = min(storage.p.high, max(storage.p.low, power))
    demand = sum(
        sum((b.p_mw.low + b.p_mw.high) / 2 for b in forecast_at(domain, k).loads)
        for k in range(start, end)
    ) / (end - start)
    # A zero-loss candidate is sufficient here: iterative AC loss calibration
    # and the joint task certificate decide whether the resulting strategy fits.
    needed = demand - target_p_mw - power
    indices = [i for i, r in enumerate(model.resources) if r.kind in ("wind", "pv")]
    ps, qs = list(goal.p_mw), list(goal.q_mvar)
    caps = {
        i: min(
            device_model_at(domain, k).resources[i].p.high
            for k in range(start, end + 1)
        )
        for i in indices
    }
    change = needed - sum(ps[i] for i in indices)
    margins = {i: max(0, caps[i] - ps[i]) if change >= 0 else ps[i] for i in indices}
    total = sum(margins.values())
    if total:
        fraction = min(1, abs(change) / total)
        for i in indices:
            ps[i] += (1 if change >= 0 else -1) * fraction * margins[i]
            ps[i] = min(caps[i], max(0, ps[i]))
            ratio = model.resources[i].q_over_p
            if ratio is not None:
                cap = max(0, ratio * ps[i] - model.settling_resolution)
                qs[i] = min(cap, max(-cap, qs[i]))
    return TubeCommand(p_mw=tuple(ps), q_mvar=tuple(qs))

"""Local least-squares command construction; only a full certificate permits use."""

from fractions import Fraction as F
from math import nextafter, sqrt

from .contracts_tube import TubeCommand, TubeDeviceModel, TubeState


def _l1_projection(values: list[float], radius: float) -> list[float]:
    """Euclidean projection onto the absolute-sum ball for shared Q movement."""
    if sum(abs(x) for x in values) <= radius:
        return values
    ordered = sorted((abs(x) for x in values), reverse=True)
    total, threshold = 0.0, 0.0
    for count, value in enumerate(ordered, 1):
        total += value
        candidate = (total - radius) / count
        if value > candidate:
            threshold = candidate
    return [max(0.0, abs(x) - threshold) * (1 if x >= 0 else -1) for x in values]


def approach_device_command(
    model: TubeDeviceModel,
    state: TubeState,
    target: TubeCommand,
    *,
    movement_fraction: float = 1.0,
    next_model: TubeDeviceModel | None = None,
) -> TubeCommand:
    """Project P/Q jointly without reducing unrelated resource commands.

    Bounded deterministic Dykstra iterations minimize squared displacement in
    MW/Mvar (common 1-MVA base) over convex command constraints. The iterate is
    a candidate, not an optimality or safety certificate. Full device, network
    and continuation checks remain mandatory, including unmatched P/Q dynamics.
    """
    if not 0 < movement_fraction <= 1:
        raise ValueError("command movement fraction must be in (0, 1]")
    n = len(model.resources)
    if any(len(x) != n for x in (state.p_mw, state.q_mvar, target.p_mw, target.q_mvar)):
        raise ValueError("command resource axis differs")
    if next_model is not None and tuple(
        r.resource_id for r in model.resources
    ) != tuple(r.resource_id for r in next_model.resources):
        raise ValueError("next capability resource axis differs")
    origin = [*state.p_mw, *state.q_mvar]
    desired = [*target.p_mw, *target.q_mvar]
    values = [
        x + movement_fraction * (u - x) for x, u in zip(origin, desired, strict=True)
    ]
    lows: list[float] = []
    highs: list[float] = []
    for i, r in enumerate(model.resources):
        radius = 0.98 * r.ramp_p_mw / r.alpha_p
        lo, hi = (
            max(r.p.low, state.p_mw[i] - radius),
            min(r.p.high, state.p_mw[i] + radius),
        )
        if next_model is not None:
            ceiling = next_model.resources[i].p.high
            reachable = (
                ceiling - (1 - r.alpha_p) * state.p_mw[i] - model.settling_resolution
            ) / r.alpha_p
            if state.p_mw[i] <= ceiling:
                reachable = max(ceiling, reachable)
            hi = min(hi, reachable)
        if lo > hi:
            raise ValueError(f"local construction has no P interval: {r.resource_id}")
        lows.append(lo)
        highs.append(hi)
    lows.extend(r.q.low for r in model.resources)
    highs.extend(r.q.high for r in model.resources)

    def project(v: list[float], constraint: int) -> list[float]:
        z = v.copy()
        if constraint == 0:
            return [
                min(hi, max(lo, x)) for x, lo, hi in zip(v, lows, highs, strict=True)
            ]
        if constraint == 1:
            delta = _l1_projection(
                [q - x for q, x in zip(v[n:], state.q_mvar, strict=True)],
                0.98 * model.aggregate_q_ramp_mvar / model.alpha_q,
            )
            z[n:] = [x + d for x, d in zip(state.q_mvar, delta, strict=True)]
            return z
        i, face = divmod(constraint - 2, 3)
        r = model.resources[i]
        p, q = v[i], v[n + i]
        if face == 0:
            length = sqrt(p * p + q * q)
            if length > r.s_mva:
                scale = r.s_mva * (1 - 1e-13) / length
                z[i], z[n + i] = scale * p, scale * q
        elif r.q_over_p is not None:
            sign = 1 if face == 1 else -1
            excess = sign * q - r.q_over_p * p
            if excess > 0:
                amount = excess / (1 + r.q_over_p**2)
                z[i], z[n + i] = p + r.q_over_p * amount, q - sign * amount
        return z

    corrections = [[0.0] * (2 * n) for _ in range(2 + 3 * n)]
    for _ in range(160):
        previous = values.copy()
        for j, correction in enumerate(corrections):
            shifted = [x + c for x, c in zip(values, correction, strict=True)]
            values = project(shifted, j)
            corrections[j] = [x - y for x, y in zip(shifted, values, strict=True)]
        if max(abs(x - y) for x, y in zip(previous, values, strict=True)) < 1e-12:
            break
    # Floating projection on a cone face may land one ulp outside it. Move
    # inward in the candidate; never relax the exact certificate inequalities.
    for i, r in enumerate(model.resources):
        values[i] = min(highs[i], max(lows[i], values[i]))
        cap = sqrt(max(0.0, r.s_mva**2 - values[i] ** 2))
        if r.q_over_p is not None:
            cap = min(
                cap,
                max(
                    0.0,
                    r.q_over_p * max(0.0, values[i])
                    - (1 + r.q_over_p) * model.settling_resolution,
                ),
            )
        while F(cap) ** 2 + F(values[i]) ** 2 > F(r.s_mva) ** 2 and cap > 0:
            cap = nextafter(cap, 0.0)
        if r.q_over_p is not None and F(cap) > F(r.q_over_p) * max(F(0), F(values[i])):
            cap = nextafter(cap, 0.0)
        values[n + i] = min(highs[n + i], cap, max(lows[n + i], -cap, values[n + i]))
    return TubeCommand(p_mw=tuple(values[:n]), q_mvar=tuple(values[n:]))


def approach_backup_command(
    model: TubeDeviceModel,
    state: TubeState,
    target: TubeCommand,
    *,
    movement_fraction: float = 1.0,
    next_model: TubeDeviceModel | None = None,
) -> TubeCommand:
    """Backup and economic construction share the same local projection."""
    return approach_device_command(
        model, state, target, movement_fraction=movement_fraction, next_model=next_model
    )

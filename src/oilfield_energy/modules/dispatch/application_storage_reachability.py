"""Held actual storage power with a bounded inverse first-step command.

See docs/modeling/Storage_Reachability.md. The first execution substep reaches
P(t); later substeps hold it. Energy therefore uses the same P(t) over the whole
planning interval. This is a conservative inner envelope, not a reachable set
for all possible substep trajectories or safety/feedback arbitration.
"""

from math import exp, isfinite

from .contracts import (
    StorageReachabilityEvidence,
    StorageReachabilityInput,
    StorageReachabilityRow,
)


def storage_reachability_rows(
    inputs: StorageReachabilityInput,
) -> tuple[StorageReachabilityRow, ...]:
    """Pure affine rows, shared by the convex, MILP and MISOCP adapters."""
    # Match the existing device engine's coefficient, including rounding.
    # Inputs whose coefficient rounds to zero are explicitly unsupported.
    alpha = 1.0 - exp(-inputs.execution_minutes / inputs.time_constant_minutes)
    ramp = inputs.ramp_mw_per_minute * inputs.execution_minutes
    rows: list[StorageReachabilityRow] = []
    for t in range(inputs.steps):
        if t == 0 and inputs.initial_power_mw is None:
            continue
        initial = inputs.initial_power_mw if inputs.initial_power_mw is not None else 0.0
        for sign in (-1.0, 1.0):
            for decay, limit in (
                (1.0, ramp),
                (1.0 - alpha, alpha * inputs.maximum_power_mw),
            ):
                rows.append(
                    StorageReachabilityRow(
                        t,
                        sign,
                        -sign * decay if t else 0.0,
                        limit if t else limit + sign * decay * initial,
                    )
                )
    return tuple(rows)


def assess_storage_reachability(
    inputs: StorageReachabilityInput,
    *,
    p_mw: tuple[float, ...],
    energy_mwh: tuple[float, ...],
) -> StorageReachabilityEvidence:
    """Independently replay bounded commands and signed-power energy balance.

    The checker does not reuse optimizer rows. Convex simultaneous charge and
    discharge cannot acquire an energy certificate from this net-power replay.
    """
    if (
        len(p_mw) != inputs.steps
        or len(energy_mwh) != inputs.steps + 1
        or any(not isfinite(v) for v in (*p_mw, *energy_mwh))
    ):
        raise ValueError("storage witness requires complete finite trajectories")
    alpha = 1.0 - exp(-inputs.execution_minutes / inputs.time_constant_minutes)
    ramp = inputs.ramp_mw_per_minute * inputs.execution_minutes
    previous = p_mw[0] if inputs.initial_power_mw is None else inputs.initial_power_mw
    power_error = max(0.0, abs(previous) - inputs.maximum_power_mw)
    energy_error = abs(energy_mwh[0] - inputs.initial_energy_mwh)
    commands: list[float] = []
    expected = inputs.initial_energy_mwh
    for t, power in enumerate(p_mw):
        command = previous + (power - previous) / alpha
        if not isfinite(command):
            raise ValueError("storage inverse command overflow")
        bounded = min(inputs.maximum_power_mw, max(-inputs.maximum_power_mw, command))
        increment = min(ramp, max(-ramp, alpha * (bounded - previous)))
        response = previous + increment
        power_error = max(
            power_error,
            abs(command) - inputs.maximum_power_mw,
            abs(power) - inputs.maximum_power_mw,
            abs(response - power),
            abs(power - previous) - ramp,
        )
        expected += (
            inputs.planning_minutes
            / 60
            * (-power / inputs.eta_discharge if power >= 0 else -power * inputs.eta_charge)
        )
        energy_error = max(energy_error, abs(expected - energy_mwh[t + 1]))
        commands.append(command)
        previous = power
    energy_error = max(
        energy_error,
        max(inputs.minimum_energy_mwh - v for v in energy_mwh),
        max(v - inputs.maximum_energy_mwh for v in energy_mwh),
        abs(energy_mwh[-1] - inputs.terminal_energy_mwh) - inputs.terminal_tolerance_mwh,
    )
    return StorageReachabilityEvidence(
        inputs=inputs,
        status="passed"
        if (
            power_error <= inputs.policy.power_tolerance_mw
            and energy_error <= inputs.policy.energy_tolerance_mwh
        )
        else "violated",
        p_mw=p_mw,
        energy_mwh=energy_mwh,
        first_command_mw=tuple(commands),
        maximum_power_violation_mw=power_error,
        maximum_energy_violation_mwh=energy_error,
    )

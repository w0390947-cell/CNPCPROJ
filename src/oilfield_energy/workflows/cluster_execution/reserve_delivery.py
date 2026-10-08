"""Causal reserve policy and continuous box proof, independent of plan solvers.

Every between-control power lies in the closed endpoint hull. Energy is bounded
by integrating its extreme signed derivative for the ENTIRE response + support
duration. AC existence, uniqueness, voltage and thermal limits use rational
enclosures, never endpoint sampling as a proof of an interval.
"""

# pyright: strict
from fractions import Fraction as F
from functools import lru_cache

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.modules.power_flow.api import (
    certify_ac_box,
    radial_point_response,
    refine_ac_balance_certificate,
    certified_ac_loss_bounds,
)
from oilfield_energy.modules.power_flow.contracts import (
    PowerInterval,
    ACBoxCertificate,
    RadialEnvelope,
)

from .contracts_reserve import (
    ReserveActivationCommand,
    ReserveClusterCertificate,
    ReserveServiceCertificate,
    ReserveServiceInput,
)


def _demands(
    inputs: ReserveServiceInput, power: PowerInterval
) -> tuple[PowerInterval, ...]:
    demands = list(inputs.demand_p_mw)
    demand = demands[inputs.storage_bus]
    demands[inputs.storage_bus] = PowerInterval(
        low=lower(F(demand.low) - F(power.high)),
        high=upper(F(demand.high) - F(power.low)),
    )
    return tuple(demands)


@lru_cache(maxsize=512)
def _network_box(
    network: RadialEnvelope, p: tuple[PowerInterval, ...], q: tuple[PowerInterval, ...]
) -> ACBoxCertificate:
    """Exact immutable input keys reuse only identical AC proof problems.

    Energy, clock and recovery checks remain outside this bounded cache.
    """

    def total(values: tuple[PowerInterval, ...]) -> PowerInterval:
        return PowerInterval(
            low=lower(sum((F(v.low) for v in values), F(0))),
            high=upper(sum((F(v.high) for v in values), F(0))),
        )

    return refine_ac_balance_certificate(
        network, certify_ac_box(network, p, q), total(p), total(q)
    )


def certify_reserve_service(inputs: ReserveServiceInput) -> ReserveServiceCertificate:
    """Sufficient all-calls test; failure means unproven, not physical infeasibility.

    The power box covers all targets; dynamic bounds additionally use the
    declared slew and causal predecessor. No future activation is used.
    """
    x = inputs
    lo = F(lower(F(x.baseline_power_mw.low) - F(x.activation_headroom_mw)))
    hi = F(upper(F(x.baseline_power_mw.high) + F(x.activation_headroom_mw)))
    initial = F(x.initial_power_mw)
    roundoff = F(x.policy.response_roundoff_mw)
    plow, phigh = min(lo - roundoff, initial), max(hi + roundoff, initial)
    box = PowerInterval(low=lower(plow), high=upper(phigh))
    alpha = F(x.response_alpha)
    commands = PowerInterval(
        low=lower((lo - (1 - alpha) * phigh) / alpha),
        high=upper((hi - (1 - alpha) * plow) / alpha),
    )
    duration = F(x.support_minutes + x.policy.response_minutes, 60)
    initial_energy = x.initial_energy_bounds_mwh or PowerInterval(
        low=x.initial_energy_mwh, high=x.initial_energy_mwh
    )
    energy = PowerInterval(
        low=lower(
            F(initial_energy.low) - max(F(0), phigh) * duration / F(x.eta_discharge)
        ),
        high=upper(
            F(initial_energy.high) + max(F(0), -plow) * duration * F(x.eta_charge)
        ),
    )
    reasons: list[str] = []
    for resource in x.other_resources:
        p = max(abs(F(resource.p_mw.low)), abs(F(resource.p_mw.high)))
        q = max(abs(F(resource.q_mvar.low)), abs(F(resource.q_mvar.high)))
        if p * p + q * q > F(resource.s_mva) ** 2 or (
            resource.q_over_p is not None
            and q > max(F(0), F(resource.p_mw.low)) * F(resource.q_over_p)
        ):
            reasons.append(f"baseline device P/Q capacity: {resource.resource_id}")
    if max(abs(plow), abs(phigh)) > F(x.maximum_power_mw):
        reasons.append("storage power")
    change = max(hi - plow, phigh - lo)
    if (
        max(abs(plow), abs(phigh)) ** 2
        + max(abs(F(x.storage_q_mvar.low)), abs(F(x.storage_q_mvar.high))) ** 2
        > F(x.storage_s_mva) ** 2
    ):
        reasons.append("storage P/Q capacity")
    if energy.low < x.minimum_energy_mwh or energy.high > x.maximum_energy_mwh:
        reasons.append("full response/support energy")
    standby = F(x.required_mw) * F(x.support_minutes, 60)
    if F(initial_energy.low) < F(x.minimum_energy_mwh) + standby / F(
        x.eta_discharge
    ) or F(initial_energy.high) > F(x.maximum_energy_mwh) - standby * F(x.eta_charge):
        reasons.append("standby energy not restored")
    if reasons:
        uncomputed = ACBoxCertificate(
            certified=False,
            reason="device/energy precondition unproven; AC not computed",
        )
        return ReserveServiceCertificate(
            inputs=x,
            status="unknown",
            reason="; ".join(reasons),
            storage_power_mw=box,
            energy_mwh=energy,
            command_mw=commands,
            maximum_change_mw=upper(change),
            network=uncomputed,
            nominal_network=uncomputed,
            up_endpoint=uncomputed,
            down_endpoint=uncomputed,
        )
    network = _network_box(x.network, _demands(x, box), x.demand_q_mvar)
    nominal = _network_box(x.network, _demands(x, x.baseline_power_mw), x.demand_q_mvar)
    up = _network_box(
        x.network,
        _demands(x, PowerInterval(low=upper(hi), high=upper(hi))),
        x.demand_q_mvar,
    )
    down = _network_box(
        x.network,
        _demands(x, PowerInterval(low=lower(lo), high=lower(lo))),
        x.demand_q_mvar,
    )
    if network.certified:
        losses, _ = certified_ac_loss_bounds(x.network, network)
        width = F(losses.high) - F(losses.low)
        slew = F(x.required_mw) * F(x.policy.activation_slew_fraction_per_minute)
        tolerance = F(x.policy.pcc_tolerance_mw)
        baseline_width = F(x.baseline_power_mw.high) - F(x.baseline_power_mw.low)
        initial_gap = max(
            abs(F(x.baseline_power_mw.low) - initial),
            abs(F(x.baseline_power_mw.high) - initial),
        )
        # y=b+a+(L(y)-L(b))+error; compare consecutive causal root responses.
        change = min(
            change,
            max(
                initial_gap + slew + width + tolerance,
                baseline_width + slew + 2 * (width + tolerance),
            )
            + 2 * roundoff,
        )
        commands = PowerInterval(
            low=max(commands.low, lower(lo - (1 - alpha) * change / alpha)),
            high=min(commands.high, upper(hi + (1 - alpha) * change / alpha)),
        )
    if max(abs(F(commands.low)), abs(F(commands.high))) > F(x.maximum_power_mw):
        reasons.append("response command/power")
    if change > F(x.ramp_mw_per_minute):
        reasons.append("one-minute ramp")
    if not all(c.certified for c in (network, nominal, up, down)):
        reasons.append("continuous AC box: " + network.reason)
    if nominal.pcc_p_mw is None or up.pcc_p_mw is None or down.pcc_p_mw is None:
        reasons.append("PCC delivery unavailable")
    elif F(nominal.pcc_p_mw.low) - F(up.pcc_p_mw.high) < F(x.required_mw) or F(
        down.pcc_p_mw.low
    ) - F(nominal.pcc_p_mw.high) < F(x.required_mw):
        reasons.append("PCC delivery bracket")
    return ReserveServiceCertificate(
        inputs=x,
        status="unknown" if reasons else "certified",
        reason="; ".join(reasons),
        storage_power_mw=box,
        energy_mwh=energy,
        command_mw=commands,
        maximum_change_mw=upper(change),
        network=network,
        nominal_network=nominal,
        up_endpoint=up,
        down_endpoint=down,
    )


def certify_cluster_reserve(
    rows: tuple[ReserveServiceCertificate, ...],
    *,
    import_limit_mw: float,
    pf_target: float,
) -> ReserveClusterCertificate:
    """Cartesian product of all regional calls; no same-direction assumption."""
    if (
        not rows
        or len({r.inputs.region for r in rows}) != len(rows)
        or len({r.inputs.start_minute for r in rows}) != 1
        or len(
            {(r.inputs.support_minutes, r.inputs.policy.response_minutes) for r in rows}
        )
        != 1
    ):
        raise ValueError("cluster reserve certificate requires aligned unique regions")
    if import_limit_mw <= 0 or not 0 < pf_target <= 1:
        raise ValueError("invalid cluster reserve limits")
    reasons = [
        f"{r.inputs.region}: {r.reason}" for r in rows if r.status != "certified"
    ]
    p_lo = p_hi = q_lo = q_hi = F(0)
    complete = True
    for row in rows:
        p, q = row.network.pcc_p_mw, row.network.pcc_q_mvar
        if p is None or q is None:
            complete = False
            reasons.append("missing regional PCC enclosure")
            continue
        p_lo += F(p.low)
        p_hi += F(p.high)
        q_lo += F(q.low)
        q_hi += F(q.high)
    if p_hi > F(import_limit_mw):
        reasons.append("joint import capacity")
    pf = F(pf_target)
    if p_lo < 0 or max(abs(q_lo), abs(q_hi)) ** 2 * pf**2 > p_lo**2 * (1 - pf**2):
        reasons.append("joint power factor")
    return ReserveClusterCertificate(
        start_minute=rows[0].inputs.start_minute,
        status="unknown" if reasons else "certified",
        reason="; ".join(reasons),
        regions=rows,
        import_upper_mw=upper(p_hi) if complete else None,
        import_limit_mw=import_limit_mw,
        pf_target=pf_target,
    )


def reserve_activation_command(
    certificate: ReserveServiceCertificate,
    *,
    demand_p_mw: tuple[float, ...],
    demand_q_mvar: tuple[float, ...],
    baseline_power_mw: float,
    previous_power_mw: float,
    requested_pcc_up_mw: float,
    previous_request_mw: float,
    elapsed_minutes: int,
    previous_command: ReserveActivationCommand | None = None,
) -> ReserveActivationCommand:
    """Executable causal recourse: current known request -> AC root -> lag inverse.

    Demand inputs are the next endpoint of the announced baseline, known from
    current state and its deterministic response model (not future telemetry).
    Unannounced within-interval errors retain within-box network safety, but not
    the same PCC tracking promise. No future activation request is read.
    """
    x = certificate.inputs
    if (
        certificate.status != "certified"
        or not 0 <= elapsed_minutes <= x.support_minutes
    ):
        raise ValueError("inactive or uncertified reserve service")
    if elapsed_minutes == 0:
        if (
            previous_command is not None
            or previous_request_mw != 0
            or previous_power_mw != x.initial_power_mw
        ):
            raise ValueError(
                "reserve activation must start at its certified actual state"
            )
    elif (
        previous_command is None
        or previous_command.context_id != x.context_id
        or previous_command.elapsed_minutes + 1 != elapsed_minutes
        or previous_command.requested_pcc_up_mw != previous_request_mw
        or abs(previous_command.target_storage_mw - previous_power_mw)
        > x.policy.response_roundoff_mw
    ):
        raise ValueError(
            "reserve activation requires the preceding causal command and actual response"
        )
    if len(demand_p_mw) != len(x.demand_p_mw) or len(demand_q_mvar) != len(
        x.demand_q_mvar
    ):
        raise ValueError("reserve activation bus axis differs")
    if (
        any(
            not b.contains(v)
            for b, v in zip(
                x.demand_p_mw + x.demand_q_mvar,
                demand_p_mw + demand_q_mvar,
                strict=True,
            )
        )
        or not x.baseline_power_mw.contains(baseline_power_mw)
        or not certificate.storage_power_mw.contains(previous_power_mw)
    ):
        raise ValueError("reserve activation departed from certified context")
    if (
        abs(requested_pcc_up_mw) > x.required_mw
        or abs(previous_request_mw) > x.required_mw
        or abs(requested_pcc_up_mw - previous_request_mw)
        > x.required_mw * x.policy.activation_slew_fraction_per_minute
    ):
        raise ValueError("reserve activation outside amplitude/slew domain")

    def response(power: float) -> float:
        p = list(demand_p_mw)
        p[x.storage_bus] -= power
        return radial_point_response(x.network, tuple(p), demand_q_mvar)[0]

    baseline = response(baseline_power_mw)
    target = baseline - requested_pcc_up_mw
    lo = x.baseline_power_mw.low - x.activation_headroom_mw
    hi = x.baseline_power_mw.high + x.activation_headroom_mw
    if response(lo) < target or response(hi) > target:
        raise ValueError("reserve root bracket not realised")
    for _ in range(80):
        middle = (lo + hi) / 2
        if response(middle) > target:
            lo = middle
        else:
            hi = middle
    selected = (lo + hi) / 2
    # Floating root finding is only a proposal: check its actual PCC interval.
    p = [PowerInterval(low=v, high=v) for v in demand_p_mw]
    p[x.storage_bus] = PowerInterval(
        low=lower(
            F(demand_p_mw[x.storage_bus])
            - F(selected)
            - F(x.policy.response_roundoff_mw)
        ),
        high=upper(
            F(demand_p_mw[x.storage_bus])
            - F(selected)
            + F(x.policy.response_roundoff_mw)
        ),
    )
    q = tuple(PowerInterval(low=v, high=v) for v in demand_q_mvar)
    checked = _network_box(x.network, tuple(p), q)
    base_p = [PowerInterval(low=v, high=v) for v in demand_p_mw]
    base_p[x.storage_bus] = PowerInterval(
        low=lower(F(demand_p_mw[x.storage_bus]) - F(baseline_power_mw)),
        high=upper(F(demand_p_mw[x.storage_bus]) - F(baseline_power_mw)),
    )
    base = _network_box(x.network, tuple(base_p), q)
    if (
        not checked.certified
        or not base.certified
        or checked.pcc_p_mw is None
        or base.pcc_p_mw is None
        or max(
            abs(
                F(base.pcc_p_mw.low) - F(checked.pcc_p_mw.high) - F(requested_pcc_up_mw)
            ),
            abs(
                F(base.pcc_p_mw.high) - F(checked.pcc_p_mw.low) - F(requested_pcc_up_mw)
            ),
        )
        > F(x.policy.pcc_tolerance_mw)
    ):
        raise ValueError("reserve root delivery was not independently certified")
    command = previous_power_mw + (selected - previous_power_mw) / x.response_alpha
    if (
        abs(command) > x.maximum_power_mw
        or abs(selected - previous_power_mw) > x.ramp_mw_per_minute
    ):
        raise ValueError("reserve command is not dynamically executable")
    return ReserveActivationCommand(
        context_id=x.context_id,
        requested_pcc_up_mw=requested_pcc_up_mw,
        target_storage_mw=selected,
        command_storage_mw=command,
        pcc_response_mw=response(selected),
        elapsed_minutes=elapsed_minutes,
    )

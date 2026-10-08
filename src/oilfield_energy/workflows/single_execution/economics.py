"""Reprice the admitted feedback prefix on the same declared forecast basis."""

from fractions import Fraction as F

from oilfield_energy.modules.control.api import (
    balance_policy_command,
    execute_tube_command,
    point_box,
)
from oilfield_energy.modules.control.contracts import TubeState
from oilfield_energy.modules.dispatch.api import evaluate_economics
from oilfield_energy.modules.power_flow.api import certified_ac_loss_bounds
from oilfield_energy.modules.power_flow.contracts import PowerInterval

from .contracts_unified import UnifiedDomain, UnifiedPlanCertificate
from .network import device_model_at, forecast_at, unified_network_box


def admitted_policy_cost(
    domain: UnifiedDomain, plan: UnifiedPlanCertificate
) -> float | None:
    """Forecast-midpoint cost estimate, not realized cost or a robust cost bound.

    Renewable opportunity is the firm envelope. Extra availability would add
    the same curtailment constant to every same-window candidate. The agreed
    whole-study economic KPI is evaluated separately with its original basis.
    """
    if domain.economic_rates is None or plan.tracking is None:
        return None
    from_state = plan.initial
    if any(
        x.low != x.high
        for x in (*from_state.p_mw, *from_state.q_mvar, from_state.energy_mwh)
    ):
        raise ValueError("repricing requires the completed point state")

    state = TubeState(
        p_mw=tuple(x.low for x in from_state.p_mw),
        q_mvar=tuple(x.low for x in from_state.q_mvar),
        energy_mwh=from_state.energy_mwh.low,
    )
    prices: list[float] = []
    imports: list[float] = []
    curtailed: list[float] = []
    charge: list[float] = []
    discharge: list[float] = []
    losses: list[float] = []
    storage = next(
        i for i, r in enumerate(domain.devices.resources) if r.kind == "storage"
    )
    for offset, policy in enumerate(plan.tracking.policies):
        k = plan.start_step + offset
        f = forecast_at(domain, k)
        loads = tuple(
            b.model_copy(
                update={
                    "p_mw": PowerInterval(
                        low=(b.p_mw.low + b.p_mw.high) / 2,
                        high=(b.p_mw.low + b.p_mw.high) / 2,
                    ),
                    "q_mvar": PowerInterval(
                        low=(b.q_mvar.low + b.q_mvar.high) / 2,
                        high=(b.q_mvar.low + b.q_mvar.high) / 2,
                    ),
                }
            )
            for b in f.loads
        )
        p = float(sum((F(b.p_mw.low) for b in loads), F(0)))
        q = float(sum((F(b.q_mvar.low) for b in loads), F(0)))
        model = device_model_at(domain, k)
        command = balance_policy_command(model, state, policy, p, q)
        state = execute_tube_command(model, state, command)
        ac = unified_network_box(
            domain, point_box(state), f.model_copy(update={"loads": loads})
        )
        if not ac.certified or ac.pcc_p_mw is None:
            raise ValueError("adopted-policy forecast repricing has no AC witness")
        lp, _ = certified_ac_loss_bounds(domain.network, ac)
        available = {
            r.resource_id: min(
                r.p.high,
                next(
                    v.minimum_available_mw
                    for v in f.generation
                    if v.resource_id == r.resource_id
                ),
            )
            for r in model.resources
            if r.kind in ("wind", "pv")
        }
        prices.append(domain.economic_prices_cny_per_mwh[k])
        imports.append((ac.pcc_p_mw.low + ac.pcc_p_mw.high) / 2)
        curtailed.append(
            sum(
                max(0, available[r.resource_id] - state.p_mw[i])
                for i, r in enumerate(model.resources)
                if r.resource_id in available
            )
        )
        charge.append(max(0, -command.p_mw[storage]))
        discharge.append(max(0, command.p_mw[storage]))
        losses.append((lp.low + lp.high) / 2)
    return evaluate_economics(
        price_cny_per_mwh=prices,
        import_mw=imports,
        curtailed_mw=curtailed,
        charge_mw=charge,
        discharge_mw=discharge,
        loss_mw=losses,
        dt_hours=domain.devices.step_minutes / 60,
        rates=domain.economic_rates,
    ).economic_cost_cny

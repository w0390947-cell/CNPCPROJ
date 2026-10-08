"""Bind nominal economic transition witnesses to the common actual-state domain."""

from oilfield_energy.modules.dispatch.api import pq_witness_violation

from .contracts_unified import SingleEconomicProposal, UnifiedDomain
from .network import device_model_at

NOMINAL_POWER_TOLERANCE = 1e-6  # MW / Mvar; numerical witness residual only.
NOMINAL_ENERGY_TOLERANCE_MWH = 1e-6


def validate_pq_proposal(
    domain: UnifiedDomain, proposal: SingleEconomicProposal
) -> None:
    w = proposal.pq_witness
    if w is None:
        if any(c.code == "JOINT_PQ_PLAN" for c in proposal.result.checks):
            raise ValueError("joint P/Q claim has no nominal transition witness")
        return
    x = w.inputs
    stop = proposal.start_step + x.planning_steps * x.planning_minutes
    if (
        x.start_step != proposal.start_step
        or x.initial != proposal.initial
        or x.devices != domain.devices
        or stop > len(domain.forecasts)
        or x.p_ceiling_mw
        != tuple(
            tuple(r.p.high for r in device_model_at(domain, k).resources)
            for k in range(proposal.start_step, stop + 1)
        )
        or pq_witness_violation(w) > NOMINAL_POWER_TOLERANCE
    ):
        raise ValueError(
            "joint P/Q nominal witness does not recompute in its actual domain"
        )
    result = proposal.result
    ids = {r.resource_id for r in x.devices.resources}
    if set(result.resource_p_mw) != ids or set(result.resource_q_mvar) != ids:
        raise ValueError("joint P/Q witness and economic resource axes differ")
    if len(result.time_minutes) != x.planning_steps:
        raise ValueError("joint P/Q witness and economic time axis differ")
    command = proposal.command
    first = w.endpoints[0]
    if (
        result.status != "passed"
        or command is None
        or len(command.p_mw) != len(first.p_mw)
        or len(command.q_mvar) != len(first.q_mvar)
        or any(
            abs(a - b) > NOMINAL_POWER_TOLERANCE
            for a, b in zip(
                (*command.p_mw, *command.q_mvar),
                (*first.p_mw, *first.q_mvar),
                strict=True,
            )
        )
    ):
        raise ValueError("joint P/Q first endpoint differs from its economic proposal")
    if proposal.target_origin == "economic" and (
        abs(proposal.target_p_mw - result.p_mw[0]) > NOMINAL_POWER_TOLERANCE
        or abs(proposal.target_q_mvar - result.q_mvar[0]) > NOMINAL_POWER_TOLERANCE
    ):
        raise ValueError("internal PCC request differs from its economic plan point")
    # Regional labels may have a declared nonzero clock origin. Their spacing
    # must still describe exactly the interval integrated by the joint model.
    if any(
        abs(minute - result.time_minutes[0] - t * x.planning_minutes) > 1e-6
        for t, minute in enumerate(result.time_minutes)
    ):
        raise ValueError("joint P/Q witness and economic interval lengths differ")
    energy = x.initial.energy_mwh
    if (
        len(result.storage_energy_mwh) != x.planning_steps + 1
        or abs(result.storage_energy_mwh[0] - energy) > NOMINAL_ENERGY_TOLERANCE_MWH
    ):
        raise ValueError("joint P/Q energy starts from a different actual state")
    for t, endpoint in enumerate(w.endpoints):
        for i, r in enumerate(x.devices.resources):
            if (
                abs(endpoint.p_mw[i] - result.resource_p_mw[r.resource_id][t])
                > NOMINAL_POWER_TOLERANCE
                or abs(endpoint.q_mvar[i] - result.resource_q_mvar[r.resource_id][t])
                > NOMINAL_POWER_TOLERANCE
            ):
                raise ValueError(
                    "joint P/Q endpoint differs from the economic schedule"
                )
            if r.kind == "storage":
                p = endpoint.p_mw[i]
                if abs(result.storage_power_mw[t] - p) > NOMINAL_POWER_TOLERANCE:
                    raise ValueError(
                        "joint P/Q PCS power differs from economic storage power"
                    )
                energy += (
                    (
                        -p / x.devices.eta_discharge
                        if p >= 0
                        else -p * x.devices.eta_charge
                    )
                    * x.planning_minutes
                    / 60
                )
        if (
            abs(result.storage_energy_mwh[t + 1] - energy)
            > NOMINAL_ENERGY_TOLERANCE_MWH
        ):
            raise ValueError("joint P/Q PCS hold and economic energy integral differ")

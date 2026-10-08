"""The same reserve product participates in joint continuation admission."""

# pyright: strict
from fractions import Fraction as F
from hashlib import sha256
from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.modules.control.contracts import TubeBox
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from .contracts_safety import ClusterSafetyDomain, ClusterForecast
from .contracts_reserve import (
    ReserveServiceInput,
    ReserveClusterCertificate,
    ReserveServiceCertificate,
)
from .reserve_delivery import certify_reserve_service, certify_cluster_reserve
from .safety_network import forecast_at, nodal_demands


def energy_band(domain: ClusterSafetyDomain, index: int) -> PowerInterval:
    r = domain.regions[index]
    if domain.reserve_policy is None or domain.delivery_policy is None:
        return PowerInterval(
            low=r.devices.energy_mwh.low, high=r.devices.energy_mwh.high
        )
    # Four rated support energies form a conservative terminal operating band;
    # actual service and its return still need independent certificates.
    amount = (
        F(4)
        * F(r.required_reserve_mw)
        * F(
            domain.reserve_policy.support_minutes
            + domain.delivery_policy.response_minutes,
            60,
        )
    )
    return PowerInterval(
        low=upper(F(r.devices.energy_mwh.low) + amount / F(r.devices.eta_discharge)),
        high=lower(F(r.devices.energy_mwh.high) - amount * F(r.devices.eta_charge)),
    )


def certify_state_reserve(
    domain: ClusterSafetyDomain, boxes: tuple[TubeBox, ...], minute: int
) -> ReserveClusterCertificate | None:
    if domain.reserve_policy is None or domain.delivery_policy is None:
        return None
    rows: list[ReserveServiceCertificate] = []
    duration = (
        domain.reserve_policy.support_minutes + domain.delivery_policy.response_minutes
    )
    for r, box in zip(domain.regions, boxes, strict=True):
        future = tuple(forecast_at(r, minute + j) for j in range(duration + 1))
        f = ClusterForecast(
            load_p_mw=tuple(
                PowerInterval(
                    low=min(x.load_p_mw[i].low for x in future),
                    high=max(x.load_p_mw[i].high for x in future),
                )
                for i in range(len(r.network.buses))
            ),
            load_q_mvar=tuple(
                PowerInterval(
                    low=min(x.load_q_mvar[i].low for x in future),
                    high=max(x.load_q_mvar[i].high for x in future),
                )
                for i in range(len(r.network.buses))
            ),
            available_mw=tuple(
                min(x.available_mw[i] for x in future)
                for i in range(len(r.devices.resources))
            ),
        )
        unavailable = any(
            x.kind in ("wind", "pv") and b.high > a
            for x, b, a in zip(
                r.devices.resources, box.p_mw, f.available_mw, strict=True
            )
        )
        p, q = nodal_demands(r, box, f, exclude_storage=True)
        i = next(i for i, x in enumerate(r.devices.resources) if x.kind == "storage")
        storage = r.devices.resources[i]
        initial = max((box.p_mw[i].low, box.p_mw[i].high), key=abs)
        trials: list[ReserveServiceCertificate] = []
        for factor in (1.05, 1.25, 1.5, 2.0, 3.0, 4.0):
            x = ReserveServiceInput(
                region=r.region,
                start_minute=minute,
                support_minutes=domain.reserve_policy.support_minutes,
                policy=domain.delivery_policy,
                network=r.network,
                demand_p_mw=p,
                demand_q_mvar=q,
                storage_bus=r.network.buses.index(storage.bus),
                baseline_power_mw=PowerInterval(low=0, high=0),
                initial_power_mw=initial,
                initial_energy_mwh=(box.energy_mwh.low + box.energy_mwh.high) / 2,
                initial_energy_bounds_mwh=box.energy_mwh,
                minimum_energy_mwh=r.devices.energy_mwh.low,
                maximum_energy_mwh=r.devices.energy_mwh.high,
                maximum_power_mw=max(abs(storage.p.low), abs(storage.p.high)),
                storage_s_mva=storage.s_mva,
                storage_q_mvar=box.q_mvar[i],
                eta_charge=r.devices.eta_charge,
                eta_discharge=r.devices.eta_discharge,
                response_alpha=storage.alpha_p,
                ramp_mw_per_minute=storage.ramp_p_mw,
                required_mw=r.required_reserve_mw,
                activation_headroom_mw=max(
                    factor * r.required_reserve_mw, abs(initial)
                ),
                context_id="pending",
            )
            x = x.model_copy(
                update={"context_id": sha256(x.model_dump_json().encode()).hexdigest()}
            )
            c = certify_reserve_service(x)
            trials.append(c)
            if c.status == "certified":
                break
        selected = next((c for c in trials if c.status == "certified"), trials[-1])
        if unavailable:
            # A recoverable absence of a full frozen-baseline reserve proof is
            # an observation, not a plant failure during the original grace period.
            selected = selected.model_copy(
                update={
                    "status": "unknown",
                    "reason": "reserve baseline exceeds future firm availability; "
                    + selected.reason,
                }
            )
        rows.append(selected)
    return certify_cluster_reserve(
        tuple(rows), import_limit_mw=domain.import_limit_mw, pf_target=domain.pf_target
    )

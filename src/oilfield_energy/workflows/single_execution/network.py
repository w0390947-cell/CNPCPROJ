"""Network and time-varying device envelopes shared by all certificates."""

from fractions import Fraction as F
from math import ceil, floor

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.modules.control.contracts import TubeBox, TubeDeviceModel
from oilfield_energy.modules.power_flow.api import (
    certify_ac_box,
    refine_ac_balance_certificate,
)
from oilfield_energy.modules.power_flow.contracts import ACBoxCertificate, PowerInterval

from .contracts_unified import SingleForecastEnvelope, UnifiedDomain


def forecast_at(domain: UnifiedDomain, step: int) -> SingleForecastEnvelope:
    return domain.forecasts[min(step, len(domain.forecasts) - 1)]


def device_model_at(domain: UnifiedDomain, step: int) -> TubeDeviceModel:
    floors = {
        r.resource_id: r.minimum_available_mw
        for r in forecast_at(domain, step).generation
    }
    return domain.devices.model_copy(
        update={
            "resources": tuple(
                r.model_copy(
                    update={
                        "p": PowerInterval(
                            low=r.p.low, high=min(r.p.high, floors[r.resource_id])
                        )
                    }
                )
                if r.resource_id in floors
                else r
                for r in domain.devices.resources
            )
        }
    )


def unified_network_box(
    domain: UnifiedDomain,
    box: TubeBox,
    loads: SingleForecastEnvelope,
    *,
    reusable: bool = False,
) -> ACBoxCertificate:
    demand = {v.bus: v for v in loads.loads}
    p: list[PowerInterval] = []
    q: list[PowerInterval] = []
    for bus in domain.network.buses:
        indices = [i for i, r in enumerate(domain.devices.resources) if r.bus == bus]
        p.append(
            PowerInterval(
                low=lower(
                    F(demand[bus].p_mw.low)
                    - sum((F(box.p_mw[i].high) for i in indices), F(0))
                ),
                high=upper(
                    F(demand[bus].p_mw.high)
                    - sum((F(box.p_mw[i].low) for i in indices), F(0))
                ),
            )
        )
        q.append(
            PowerInterval(
                low=lower(
                    F(demand[bus].q_mvar.low)
                    - sum((F(box.q_mvar[i].high) for i in indices), F(0))
                ),
                high=upper(
                    F(demand[bus].q_mvar.high)
                    - sum((F(box.q_mvar[i].low) for i in indices), F(0))
                ),
            )
        )
    net_p = PowerInterval(
        low=lower(sum((F(x.low) for x in p), F(0))),
        high=upper(sum((F(x.high) for x in p), F(0))),
    )
    net_q = PowerInterval(
        low=lower(sum((F(x.low) for x in q), F(0))),
        high=upper(sum((F(x.high) for x in q), F(0))),
    )
    if reusable:
        # Nearby proposals can share an outward rational AC box. Failure retries
        # the original tight enclosure; no physical limit or guarantee is relaxed.
        def grid(x: PowerInterval) -> PowerInterval:
            return PowerInterval(
                low=lower(F(floor(F(x.low) * 40), 40)),
                high=upper(F(ceil(F(x.high) * 40), 40)),
            )

        coarse = certify_ac_box(
            domain.network, tuple(grid(x) for x in p), tuple(grid(x) for x in q)
        )
        coarse = refine_ac_balance_certificate(domain.network, coarse, net_p, net_q)
        if coarse.certified:
            return coarse
    return refine_ac_balance_certificate(
        domain.network, certify_ac_box(domain.network, tuple(p), tuple(q)), net_p, net_q
    )

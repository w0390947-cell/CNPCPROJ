"""Explicit synthetic research inputs for joint cluster safety tests."""

from dataclasses import replace

from oilfield_energy.bootstrap.adapters.cluster_safety import (
    build_cluster_safety_domain,
)
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy
from oilfield_energy.modules.dispatch.contracts import (
    StorageReservePolicy,
    ReserveDeliveryPolicy,
)
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    ClusterSafetyPolicy,
)
from tests.support.reserve_case import stationary_reserve_case


def safety_domain(energy=2.0, *, reserve=True):
    case = stationary_reserve_case()
    case = replace(
        case,
        microgrids=[
            replace(m, storage=replace(m.storage, e_initial_mwh=energy))
            for m in case.microgrids
        ],
    )
    policy = ExecutionPolicy(
        cluster_safety=ClusterSafetyPolicy(),
        dynamic_tracking=DynamicTrackingPolicy(),
        storage_reserve=StorageReservePolicy() if reserve else None,
        reserve_delivery=ReserveDeliveryPolicy() if reserve else None,
    )
    return build_cluster_safety_domain(case, policy, True)


def observed(domain, minute=0, *, upper=False):
    rows = []
    for r in domain.regions:
        f = r.forecasts[min(minute, len(r.forecasts) - 1)]
        values = {
            key: tuple(
                PowerInterval(
                    low=x.high if upper else (x.low + x.high) / 2,
                    high=x.high if upper else (x.low + x.high) / 2,
                )
                for x in getattr(f, key)
            )
            for key in ("load_p_mw", "load_q_mvar")
        }
        rows.append(f.model_copy(update=values))
    return tuple(rows)

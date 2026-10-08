"""Full AC and joint limits, shared by ordinary, backup and terminal paths."""

# pyright: strict
from fractions import Fraction as F
from hashlib import sha256
from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.modules.control.contracts import TubeBox, TubeDeviceModel
from oilfield_energy.modules.power_flow.api import (
    certify_ac_box,
    refine_ac_balance_certificate,
)
from oilfield_energy.modules.power_flow.contracts import PowerInterval, ACBoxCertificate
from .contracts_safety import ClusterForecast, ClusterRegionDomain, ClusterSafetyDomain


def bind_cluster_domain(domain: ClusterSafetyDomain) -> ClusterSafetyDomain:
    """Canonical identity binds every assumption and initial state, not a label."""
    canonical = domain.model_copy(update={"context_id": "pending"})
    return domain.model_copy(
        update={"context_id": sha256(canonical.model_dump_json().encode()).hexdigest()}
    )


def forecast_at(region: ClusterRegionDomain, minute: int) -> ClusterForecast:
    return region.forecasts[min(minute, len(region.forecasts) - 1)]


def model_at(region: ClusterRegionDomain, minute: int) -> TubeDeviceModel:
    f = forecast_at(region, minute)
    return region.devices.model_copy(
        update={
            "resources": tuple(
                r.model_copy(
                    update={"p": PowerInterval(low=r.p.low, high=min(r.p.high, a))}
                )
                if r.kind in ("wind", "pv")
                else r
                for r, a in zip(region.devices.resources, f.available_mw, strict=True)
            )
        }
    )


def sum_interval(xs: tuple[PowerInterval, ...]) -> PowerInterval:
    return PowerInterval(
        low=lower(sum((F(x.low) for x in xs), F(0))),
        high=upper(sum((F(x.high) for x in xs), F(0))),
    )


def nodal_demands(
    region: ClusterRegionDomain,
    box: TubeBox,
    f: ClusterForecast,
    *,
    exclude_storage: bool = False,
) -> tuple[tuple[PowerInterval, ...], tuple[PowerInterval, ...]]:
    axes: list[tuple[PowerInterval, ...]] = []
    for reactive, loads, values in (
        (False, f.load_p_mw, box.p_mw),
        (True, f.load_q_mvar, box.q_mvar),
    ):
        rows: list[PowerInterval] = []
        for bus, load in zip(region.network.buses, loads, strict=True):
            ids = [
                i
                for i, r in enumerate(region.devices.resources)
                if r.bus == bus
                and not (exclude_storage and not reactive and r.kind == "storage")
            ]
            rows.append(
                PowerInterval(
                    low=lower(
                        F(load.low) - sum((F(values[i].high) for i in ids), F(0))
                    ),
                    high=upper(
                        F(load.high) - sum((F(values[i].low) for i in ids), F(0))
                    ),
                )
            )
        axes.append(tuple(rows))
    return axes[0], axes[1]


def certify_joint_boxes(
    domain: ClusterSafetyDomain, boxes: tuple[TubeBox, ...], minute: int
) -> tuple[tuple[ACBoxCertificate, ...], PowerInterval, PowerInterval]:
    if len(boxes) != len(domain.regions):
        raise ValueError("joint path region axis differs")
    certificates: list[ACBoxCertificate] = []
    for r, box in zip(domain.regions, boxes, strict=True):
        p, q = nodal_demands(r, box, forecast_at(r, minute))
        c = refine_ac_balance_certificate(
            r.network, certify_ac_box(r.network, p, q), sum_interval(p), sum_interval(q)
        )
        if not c.certified or c.pcc_p_mw is None or c.pcc_q_mvar is None:
            raise ValueError(r.region + " interval AC unproven: " + c.reason)
        certificates.append(c)
    ps = tuple(c.pcc_p_mw for c in certificates if c.pcc_p_mw is not None)
    qs = tuple(c.pcc_q_mvar for c in certificates if c.pcc_q_mvar is not None)
    p, q = sum_interval(ps), sum_interval(qs)
    if F(p.high) > F(domain.import_limit_mw):
        raise ValueError("joint continuous import capacity unproven")
    pf = F(domain.pf_target)
    if p.low < 0 or max(abs(F(q.low)), abs(F(q.high))) ** 2 * pf**2 > F(p.low) ** 2 * (
        1 - pf**2
    ):
        raise ValueError("joint continuous power factor unproven")
    return tuple(certificates), p, q

"""Independent endpoint-safe reverse-flow witness and joint enclosure checks."""

import pytest

from oilfield_energy.modules.control.api import point_box
from oilfield_energy.modules.control.contracts import (
    TubeState,
    TubeCommand,
)
from oilfield_energy.modules.power_flow.api import radial_point_response
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from oilfield_energy.workflows.cluster_execution.safety_network import (
    certify_joint_boxes,
)
from oilfield_energy.workflows.cluster_execution.safety_certificates import certify_step
from tests.support.cluster_safety import safety_domain


def test_safe_endpoints_and_old_import_bound_do_not_hide_midinterval_reverse_flow():
    domain = safety_domain(reserve=False)
    regions = []
    before = []
    after = []
    import_upper = 0.0
    for r in domain.regions:
        resources = tuple(
            x.model_copy(
                update={
                    "p": PowerInterval(low=0, high=4),
                    "s_mva": 5.0,
                    "alpha_p": 1.0,
                    "ramp_p_mw": 4.0,
                }
            )
            if x.kind in ("wind", "pv")
            else x
            for x in r.devices.resources
        )
        devices = r.devices.model_copy(update={"resources": resources})
        load = tuple(
            PowerInterval(low=4 if b == "load" else 0, high=4 if b == "load" else 0)
            for b in r.network.buses
        )
        forecast = r.forecasts[0].model_copy(
            update={
                "load_p_mw": load,
                "available_mw": tuple(
                    4.0 if x.kind in ("wind", "pv") else 0.0 for x in resources
                ),
            }
        )
        r = r.model_copy(update={"devices": devices, "forecasts": (forecast,) * 60})
        regions.append(r)
        p0 = tuple(3.0 if x.kind == "wind" else 0.0 for x in resources)
        p1 = tuple(
            1.0 if x.kind == "wind" else 2.0 if x.kind == "pv" else 0.0
            for x in resources
        )
        before.append(TubeState(p_mw=p0, q_mvar=r.initial.q_mvar, energy_mwh=2))
        after.append(TubeState(p_mw=p1, q_mvar=r.initial.q_mvar, energy_mwh=2))
        # Analytic legacy bound, no retired executor required.
        import_upper += 4 + 0.2 - sum(min(a, b) for a, b in zip(p0, p1, strict=True))
        # Monotone, no-overshoot polynomial paths with the same safe endpoints.
        t = 0.5
        p = [x.low for x in load]
        p[r.network.buses.index("load")] -= 3 - 2 * t**4
        p[r.network.buses.index("pv")] -= 2 * (1 - (1 - t) ** 4)
        actual, _ = radial_point_response(r.network, tuple(p), (0.0,) * len(p))
        assert actual < -0.7
    domain = domain.model_copy(
        update={"regions": tuple(regions), "import_limit_mw": 10.0}
    )
    for states in (before, after):
        networks, p, _ = certify_joint_boxes(
            domain, tuple(point_box(s) for s in states), 0
        )
        assert all(c.certified for c in networks) and p.high < 4
    assert import_upper < 10.0
    with pytest.raises(ValueError, match="interval AC unproven"):
        certify_step(
            domain,
            tuple(point_box(s) for s in before),
            tuple(TubeCommand(p_mw=s.p_mw, q_mvar=s.q_mvar) for s in after),
            0,
        )


@pytest.mark.parametrize("constraint", ["voltage", "thermal", "power_factor"])
def test_joint_certificate_checks_network_and_pf_constraints(constraint):
    domain = safety_domain(reserve=False)
    regions = []
    for r in domain.regions:
        if constraint == "voltage":
            net = r.network.model_copy(update={"voltage_min_pu": 0.99999})
        elif constraint == "thermal":
            net = r.network.model_copy(
                update={
                    "branches": tuple(
                        b.model_copy(update={"rating_mva": 1.0})
                        for b in r.network.branches
                    )
                }
            )
        else:
            net = r.network.model_copy(update={"pf_min": 1.0})
        regions.append(r.model_copy(update={"network": net}))
    domain = domain.model_copy(update={"regions": tuple(regions)})
    with pytest.raises(ValueError, match="unproven"):
        certify_joint_boxes(domain, tuple(point_box(r.initial) for r in regions), 0)

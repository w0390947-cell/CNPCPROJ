"""Project legacy network and resource data into immutable execution contracts."""

from math import exp

from oilfield_energy.data import MicrogridData, ProjectCase
from oilfield_energy.modules.control.contracts import (
    TubeDeviceModel,
    TubeResource,
)
from oilfield_energy.modules.power_flow.contracts import (
    EnvelopeBranch,
    PowerInterval,
    RadialEnvelope,
)
from oilfield_energy.network_model import validate_legacy_network_alignment
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
)


def assemble_single_physical_model(
    mg: MicrogridData,
    case: ProjectCase,
    policy: ExecutionPolicy,
    *,
    storage_enabled: bool,
) -> tuple[RadialEnvelope, TubeDeviceModel]:
    dynamics = policy.dynamic_tracking
    if dynamics is None or policy.device_step_minutes != 1:
        raise ValueError("unified execution needs declared one-minute dynamics")
    resolved = validate_legacy_network_alignment(mg)
    if any(b.phase_shift_degrees != 0 for b in resolved.branches):
        raise ValueError("unified envelope does not support phase shifting")
    buses = tuple(b.bus_id for b in resolved.buses)
    network = RadialEnvelope(
        network_id=f"{mg.name}:{resolved.operating_mode_id}",
        buses=buses,
        branches=tuple(
            EnvelopeBranch(
                parent=buses.index(b.parent),
                child=buses.index(b.child),
                r_pu=b.r_pu,
                x_pu=b.x_pu,
                tap=b.tap_ratio,
                rating_mva=b.s_max_mva,
            )
            for b in resolved.branches
        ),
        slack=buses.index(resolved.pcc_bus_id),
        base_mva=resolved.base_mva,
        slack_voltage_pu=1.0,
        shunt_q_mvar=tuple(resolved.shunt_q_nominal_mvar_by_bus[b] for b in buses),
        voltage_min_pu=mg.voltage_min_pu,
        voltage_max_pu=mg.voltage_max_pu,
        pcc_min_mw=max(0.0, mg.p_grid_min_mw, case.assumptions.no_reverse_margin_mw),
        pcc_max_mw=mg.p_grid_max_mw,
        pf_min=case.assumptions.pf_min,
    )
    resources: list[TubeResource] = []
    aq = 1 - exp(-1 / dynamics.time_constant_minutes)
    for bus, rated in mg.wind_capacity_mw.items():
        rid = mg.resource_id("wind", bus)
        cap = mg.wind_reactive_capability(bus)
        bound = cap.absolute_limit_mvar
        resources.append(
            TubeResource(
                resource_id=rid,
                bus=bus,
                kind="wind",
                p=PowerInterval(low=0, high=rated),
                q=PowerInterval(low=-bound, high=bound),
                s_mva=cap.s_max_mva,
                q_over_p=cap.q_abs_over_p_max,
                alpha_p=aq,
                ramp_p_mw=dynamics.p_ramp_mw_per_minute / max(1, len(mg.wind_capacity_mw)),
            )
        )
    for bus, rated in mg.pv_capacity_mw.items():
        rid = mg.resource_id("pv", bus)
        s = mg.pv_capacity_mva[bus]
        q = s if mg.pv_can_control_reactive(bus) else 0.0
        resources.append(
            TubeResource(
                resource_id=rid,
                bus=bus,
                kind="pv",
                p=PowerInterval(low=0, high=rated),
                q=PowerInterval(low=-q, high=q),
                s_mva=s,
                alpha_p=1 - exp(-1 / dynamics.pv_time_constant_minutes),
                ramp_p_mw=dynamics.p_ramp_mw_per_minute
                * rated
                / max(sum(mg.pv_capacity_mw.values()), 1e-12),
            )
        )
    st = mg.storage
    sp = st.p_max_mw if storage_enabled else 0.0
    sq = st.s_max_mva if storage_enabled and mg.storage_reactive_enabled else 0.0
    resources.append(
        TubeResource(
            resource_id=mg.resource_id("storage", st.bus),
            bus=st.bus,
            kind="storage",
            p=PowerInterval(low=-sp, high=sp),
            q=PowerInterval(low=-sq, high=sq),
            s_mva=st.s_max_mva,
            alpha_p=1,
            ramp_p_mw=dynamics.p_ramp_mw_per_minute,
        )
    )
    svg = mg.svg_capability()
    resources.append(
        TubeResource(
            resource_id=mg.resource_id("svg", mg.svg_bus),
            bus=mg.svg_bus,
            kind="svg",
            p=PowerInterval(low=0, high=0),
            q=PowerInterval(
                low=svg.effective_q_min_mvar,
                high=svg.effective_q_max_mvar,
            ),
            s_mva=svg.s_max_mva,
            alpha_p=1,
            ramp_p_mw=0,
        )
    )
    devices = TubeDeviceModel(
        resources=tuple(resources),
        alpha_q=aq,
        aggregate_q_ramp_mvar=dynamics.q_ramp_mvar_per_minute,
        step_minutes=1,
        energy_mwh=PowerInterval(low=st.e_min_mwh, high=st.e_max_mwh),
        eta_charge=st.eta_charge,
        eta_discharge=st.eta_discharge,
        context_id="pending",
    )
    return network, devices

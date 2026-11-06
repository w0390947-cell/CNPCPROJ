"""8.3 mechanism checks derive all physical objects from the common 8.1 data.

These are scenario-specific checks, not claims that a static solve demonstrates
minute execution. Tiny historical fixtures remain independent unit regressions.
"""

import numpy as np
import pytest

from oilfield_energy.ac_power_flow import validate_ac_dispatch
from oilfield_energy.admm import run_admm_coordination
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    RegionalPlanRequest,
    realize_regional_plan,
)
from oilfield_energy.bootstrap.adapters.cluster_joint_recovery import (
    solve_joint_reference,
)
from oilfield_energy.bootstrap.adapters.cluster_safety import (
    build_cluster_safety_domain,
)
from oilfield_energy.bootstrap.adapters.cluster_reserve import with_storage_reserves
from oilfield_energy.bootstrap.adapters.cluster_study import (
    build_cluster_study,
    mechanism_study_spec,
    study_delivery_policy,
)
from oilfield_energy.hierarchy_types import ADMMConfig, PhysicalCoordinationReference
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.modules.control.api import (
    point_box,
    certify_cluster_device_transition,
)
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy, TubeCommand
from oilfield_energy.modules.dispatch.api import (
    plan_storage_reserve,
    observe_reserve_recovery,
)
from oilfield_energy.modules.dispatch.contracts import (
    NetworkCoordinationPolicy,
    StorageReservePolicy,
    PCCTrackingLimits,
)
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from oilfield_energy.workflows.cluster_execution.api import (
    ClusterSafetyController,
    replay_cluster_safety,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    ClusterSafetyPolicy,
)
from oilfield_energy.workflows.cluster_execution.safety_network import (
    certify_joint_boxes,
)


def case(section):
    return build_cluster_study(mechanism_study_spec(section))


def policy(section):
    return ExecutionPolicy(
        cluster_safety=ClusterSafetyPolicy(),
        dynamic_tracking=DynamicTrackingPolicy(),
        storage_reserve=StorageReservePolicy(),
        reserve_delivery=study_delivery_policy(mechanism_study_spec(section)),
        network_coordination=NetworkCoordinationPolicy(),
    )


def domain(section):
    return build_cluster_safety_domain(case(section), policy(section), True)


def observed(d, minute, upper=False):
    return tuple(
        r.forecasts[min(minute, len(r.forecasts) - 1)].model_copy(
            update={
                key: tuple(
                    PowerInterval(
                        low=x.high if upper else (x.low + x.high) / 2,
                        high=x.high if upper else (x.low + x.high) / 2,
                    )
                    for x in getattr(
                        r.forecasts[min(minute, len(r.forecasts) - 1)], key
                    )
                )
                for key in ("load_p_mw", "load_q_mvar")
            }
        )
        for r in d.regions
    )


def test_831_original_deadline_arithmetic_uses_each_base_storage():
    c, d = case("8.3.1"), domain("8.3.1")
    for mg, region in zip(c.microgrids, d.regions, strict=True):
        st, energy = mg.storage, mg.storage.e_initial_mwh
        for minute in (0, 5, 10):
            plan = plan_storage_reserve(
                forecast_scale_mw=(0.0,) * 3,
                step_minutes=5,
                remaining_day_minutes=60,
                initial_energy_mwh=energy,
                minimum_energy_mwh=st.e_min_mwh,
                maximum_energy_mwh=st.e_max_mwh,
                maximum_power_mw=st.p_max_mw,
                eta_charge=st.eta_charge,
                eta_discharge=st.eta_discharge,
                policy=StorageReservePolicy(
                    minimum_error_mw=region.required_reserve_mw
                ),
                recovery_remaining_minutes=15 - minute,
                full_support=True,
            )
            energy = plan.energy_floor_mwh[1]
        assert energy == pytest.approx(
            st.e_min_mwh + region.required_reserve_mw * 0.25 / st.eta_discharge
        )


def test_832_network_bottleneck_enters_coordination_and_strict_regional_realization():
    c = case("8.3.2")
    ref = run_admm_coordination(
        c,
        storage_enabled=False,
        admm_config=ADMMConfig(
            max_iterations=1000, network_model="branch-flow-socp-v1"
        ),
    )
    assert ref.converged
    settings = ExecutionPolicy(network_coordination=NetworkCoordinationPolicy())
    for mg in c.microgrids:
        row, plan = realize_regional_plan(
            RegionalPlanRequest(
                c,
                mg.name,
                ref.p_references_mw[mg.name],
                ref.q_references_mvar[mg.name],
                False,
                60,
                settings,
            )
        )
        assert row.executable_plan, row.reason
        assert plan is not None
        if mg.name == "SC":
            wind_ids = [
                r.resource_id for r in mg.resource_identities if r.kind == "wind"
            ]
            assert sum(row.resource_p_mw[rid][0] for rid in wind_ids) < sum(
                v[0] for v in mg.wind_available_mw.values()
            )


def test_833_joint_physical_search_finds_a_plan_outside_the_finite_regional_pool():
    c = case("8.3.3")
    pool = {}
    for mg in c.microgrids:
        # One independently certified, high-import candidate per region. There
        # is only one Cartesian combination, and it violates the joint limit.
        local = solve_case_misocp(
            c,
            [mg.name],
            storage_enabled=False,
            cluster_coordination=False,
            pcc_targets={mg.name: {"p_mw": np.array([18.0]), "q_mvar": np.zeros(1)}},
            exact_ac_physics=True,
            time_limit_seconds=60,
        )
        assert local.success and validate_ac_dispatch(c, local, [mg.name])["passed"]
        pool[mg.name] = local.microgrids[mg.name]
    assert sum(row["p_grid_mw"][0] for row in pool.values()) > c.cluster_import_limit_mw
    bad = PhysicalCoordinationReference(
        p_references_mw={name: row["p_grid_mw"] for name, row in pool.items()},
        q_references_mvar={name: row["q_grid_mvar"] for name, row in pool.items()},
    )
    settings = ExecutionPolicy(network_coordination=NetworkCoordinationPolicy())
    ref, evidence = solve_joint_reference(c, bad, settings, False, 0.95, (0.0,))
    assert ref is not None and evidence.status == "certified", evidence.reason
    rows = []
    for m in c.microgrids:
        row, _ = realize_regional_plan(
            RegionalPlanRequest(
                c,
                m.name,
                ref.p_references_mw[m.name],
                ref.q_references_mvar[m.name],
                False,
                60,
                settings,
            )
        )
        assert row.executable_plan, row.reason
        rows.append(row)
    assert sum(r.p_mw[0] for r in rows) <= c.cluster_import_limit_mw + 1e-6


def test_834_exact_ac_physical_phases_keep_original_network_and_storage_bounds():
    c = case("8.3.4")
    witness = solve_case_misocp(c, exact_ac_physics=True, time_limit_seconds=60)
    assert witness.success
    targets = {
        name: {"p_mw": row["p_grid_mw"], "q_mvar": row["q_grid_mvar"]}
        for name, row in witness.microgrids.items()
    }
    limits = PCCTrackingLimits(0.05, 0.05, 1e-6)
    for objective in ("feasibility", "original"):
        result = solve_case_misocp(
            c,
            exact_ac_physics=True,
            objective_mode=objective,
            time_limit_seconds=60,
            pcc_targets=targets,
            tracking_limits=limits,
        )
        assert result.success
        assert validate_ac_dispatch(c, result, [m.name for m in c.microgrids])["passed"]
        assert result.cluster["network_physics"] == "branch-flow-equality"
        for mg in c.microgrids:
            energy = result.microgrids[mg.name]["storage_energy_mwh"]
            assert min(energy) >= mg.storage.e_min_mwh - 1e-6
            assert max(energy) <= mg.storage.e_max_mwh + 1e-6
            assert (
                abs(
                    result.microgrids[mg.name]["p_grid_mw"][0]
                    - targets[mg.name]["p_mw"][0]
                )
                <= 0.05 + 1e-6
            )
            assert (
                abs(
                    result.microgrids[mg.name]["q_grid_mvar"][0]
                    - targets[mg.name]["q_mvar"][0]
                )
                <= 0.05 + 1e-6
            )


def test_8331_storage_recovery_charge_is_inside_the_same_network_plan():
    tasks = {
        (r.region, direction): observe_reserve_recovery(
            None,
            region=r.region,
            direction=direction,
            minute=0,
            required_mw=r.required_reserve_mw,
            recovery_minutes=15,
            adequate=False,
        )
        for r in domain("8.3.3.1").regions
        for direction in ("up", "down")
    }
    c = with_storage_reserves(
        case("8.3.3.1"), StorageReservePolicy(), 60, full_support=True, tasks=tasks
    )
    result = solve_case_misocp(c, exact_ac_physics=True, time_limit_seconds=60)
    assert result.success
    assert validate_ac_dispatch(c, result, [m.name for m in c.microgrids])["passed"]
    for mg in c.microgrids:
        row = result.microgrids[mg.name]
        assert row["storage_charge_mw"][0] > 0
        assert (
            row["storage_energy_mwh"][-1]
            >= c.storage_reserves[mg.name].energy_floor_mwh[-1] - 1e-6
        )
    assert (
        sum(result.microgrids[m.name]["p_grid_mw"][0] for m in c.microgrids)
        <= c.cluster_import_limit_mw + 1e-6
    )


def test_835_rejected_economic_candidates_keep_real_states_and_original_deadlines():
    d = domain("8.3.5")
    ctrl = ClusterSafetyController(d)
    bad = tuple(
        TubeCommand(p_mw=(99.0,) * len(r.devices.resources), q_mvar=r.initial.q_mvar)
        for r in d.regions
    )
    assert ctrl.tasks
    for minute in range(100):
        step = ctrl.advance(
            observed(d, minute, upper=minute % 2 == 0), bad if minute % 5 == 0 else None
        )
        assert step.source in {"backup", "terminal"}
        assert all(c.certified for c in step.path.networks)
    assert all(
        t.deadline_minute == 15 and t.completed_minute <= 15 and t.status == "recovered"
        for t in ctrl.tasks.values()
    )
    assert ctrl.reserve_call_ready
    assert replay_cluster_safety(ctrl.evidence()) == 100


def test_836_multi_region_reversing_calls_and_certified_return():
    d = domain("8.3.6")
    ctrl = ClusterSafetyController(d)
    while not ctrl.reserve_call_ready:
        ctrl.advance(observed(d, ctrl.minute), None)
        assert ctrl.minute < 90
    event = ctrl.accept_reserve_event()
    pattern = (0.0, 0.5, 1.0, 0.5, 0.0, -0.5, -1.0, -0.5)
    while ctrl.minute < event.end_minute:
        requests = tuple(
            pattern[(ctrl.minute - event.start_minute) % len(pattern)]
            * r.required_reserve_mw
            * (-1 if i == 1 else 1)
            for i, r in enumerate(d.regions)
        )
        ctrl.advance(observed(d, ctrl.minute), None, reserve_requests=requests)
    while not ctrl.reserve_call_ready:
        ctrl.advance(observed(d, ctrl.minute), None)
        assert ctrl.minute < event.end_minute + 90
    assert replay_cluster_safety(ctrl.evidence()) == ctrl.minute


def test_838_rejects_outside_domain_and_joint_overload_without_state_advance():
    d = domain("8.3.8")
    ctrl = ClusterSafetyController(d)
    rows = list(observed(d, 0))
    loads = list(rows[0].load_p_mw)
    loads[1] = PowerInterval(low=999, high=999)
    rows[0] = rows[0].model_copy(update={"load_p_mw": tuple(loads)})
    with pytest.raises(ValueError, match="declared load domain"):
        ctrl.advance(tuple(rows), None)
    assert ctrl.minute == 0 and ctrl.states == tuple(r.initial for r in d.regions)
    boxes = tuple(point_box(s) for s in ctrl.states)
    with pytest.raises(ValueError, match="joint continuous import capacity"):
        certify_joint_boxes(d.model_copy(update={"import_limit_mw": 1.0}), boxes, 0)


def test_838_reversal_energy_uses_actual_base_device_efficiencies_and_internal_extremum():
    r = domain("8.3.8").regions[0]
    index = next(i for i, x in enumerate(r.devices.resources) if x.kind == "storage")
    p = list(r.initial.p_mw)
    p[index] = -0.2
    before = r.initial.model_copy(update={"p_mw": tuple(p)})
    command = list(p)
    alpha = r.devices.resources[index].alpha_p
    command[index] = (0.2 - (1 - alpha) * (-0.2)) / alpha
    result = certify_cluster_device_transition(
        r.devices,
        point_box(before),
        TubeCommand(p_mw=tuple(command), q_mvar=before.q_mvar),
    )
    expected = (
        before.energy_mwh
        + (0.2 * 0.25 / 60) * r.devices.eta_charge
        - (0.2 * 0.25 / 60) / r.devices.eta_discharge
    )
    assert result.endpoint.energy_mwh.low == pytest.approx(expected)
    peak = before.energy_mwh + 0.2 * 0.25 / 60 * r.devices.eta_charge
    assert (
        result.path.energy_mwh.high
        >= peak
        > max(before.energy_mwh, result.endpoint.energy_mwh.high)
    )

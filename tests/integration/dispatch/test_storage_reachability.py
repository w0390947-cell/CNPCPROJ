"""Real backends: static-feasible charging cannot violate the observed ramp state."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.admm import CoordinationWorkspace
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    RegionalPlanRequest,
    diagnose_regional_reference,
    realize_regional_plan,
)
from oilfield_energy.bootstrap.adapters.cluster_rolling import with_storage_reachability
from oilfield_energy.data import (
    Line,
    MicrogridData,
    ModelAssumptions,
    ProjectCase,
    Storage,
)
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.model import solve_case
from oilfield_energy.modules.control.contracts import (
    DeviceCheckpoint,
    DynamicTrackingPolicy,
)
from oilfield_energy.modules.dispatch.contracts import (
    DispatchCapabilities,
    StorageReachabilityPolicy,
)
from oilfield_energy.network_model import build_legacy_network_model_v2
from oilfield_energy.regional_control import RegionalConvexController
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


def case_and_policy(*, initial=2.5, terminal=2.03, minutes=1.0, steps=1, enabled=True):
    buses = ["pcc", "load"]
    lines = [Line("line", "pcc", "load", 0.001, 0.002, 12.0)]
    network = build_legacy_network_model_v2(
        network_id="storage-dynamics-synthetic",
        buses=buses,
        pcc_bus_id="pcc",
        base_mva=10.0,
        lines=lines,
    )
    mg = MicrogridData(
        name="A",
        buses=buses,
        lines=lines,
        pcc_bus="pcc",
        base_mva=10.0,
        maximum_load_mw=5.0,
        p_grid_max_mw=12.0,
        p_grid_min_mw=0.15,
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        load_p_mw={"pcc": np.zeros(steps), "load": np.full(steps, 5.0)},
        load_q_mvar={b: np.zeros(steps) for b in buses},
        wind_available_mw={},
        wind_capacity_mw={},
        wind_capacity_mva={},
        pv_available_mw={},
        pv_capacity_mw={},
        pv_capacity_mva={},
        storage=Storage("load", 3.0, 4.0, 0.5, 2.0, 0.95, 0.95, 3.5, terminal),
        svg_bus="load",
        svg_q_min_mvar=0,
        svg_q_max_mvar=0,
        storage_reactive_enabled=False,
        network_model_v2=network,
        network_operating_mode_id=network.default_operating_mode_id,
    )
    case = ProjectCase(
        np.arange(steps) * minutes / 60,
        np.full(steps, 600.0),
        [mg],
        replace(ModelAssumptions(), dt_hours=minutes / 60),
        20.0,
    )
    policy = ExecutionPolicy(
        dynamic_tracking=DynamicTrackingPolicy(),
        storage_reachability=StorageReachabilityPolicy() if enabled else None,
    )
    if enabled:
        case = with_storage_reachability(
            case,
            policy=policy.storage_reachability,
            dynamics=policy.dynamic_tracking,
            checkpoints={
                "A": DeviceCheckpoint(
                    minute=0, storage_energy_mwh=2.0, storage_power_mw=initial
                )
            },
            storage_enabled=True,
            execution_step_minutes=1,
        )
    return case, policy


def request(case, policy):
    return RegionalPlanRequest(
        case,
        "A",
        np.full(len(case.time_hours), 7.0),
        np.zeros(len(case.time_hours)),
        True,
        15,
        policy,
    )


def test_static_plan_passes_but_current_discharge_makes_even_diagnostic_energy_infeasible():
    old_case, old_policy = case_and_policy(enabled=False)
    old, _ = realize_regional_plan(request(old_case, old_policy))
    assert old.executable_plan and old.storage_power_mw[0] < -1.9
    assert old.storage_energy_mwh[-1] >= 1.98
    case, policy = case_and_policy()
    row, plan = realize_regional_plan(request(case, policy))
    assert plan is None and not row.executable_plan
    assert row.solver_stop_reason == "solver_infeasible"
    diagnostic = diagnose_regional_reference((request(case, policy), 1))
    assert diagnostic.calibration is None and diagnostic.evidence.status == "violated"


@pytest.mark.parametrize("backend", ["linear", "misocp", "regional"])
def test_all_three_backends_reject_the_terminal_energy_ramp_contradiction(backend):
    case, _ = case_and_policy()
    if backend == "regional":
        with pytest.raises(RuntimeError, match="infeasible"):
            RegionalConvexController(case, case.microgrids[0], ADMMConfig()).solve(
                None, fallback=True
            )
    else:
        solver = solve_case if backend == "linear" else solve_case_misocp
        result = solver(case, ["A"], time_limit_seconds=15)
        assert not result.success


def test_changing_only_actual_initial_power_makes_the_same_reference_reachable():
    case, policy = case_and_policy(initial=-2.0)
    row, plan = realize_regional_plan(request(case, policy))
    assert plan is not None and row.executable_plan, row
    assert row.storage_reachability.status == "passed"
    assert row.storage_reachability.inputs.initial_power_mw == -2.0
    assert abs(row.storage_reachability.first_command_mw[0]) <= 3.0 + 1e-6
    assert row.storage_energy_mwh[-1] == pytest.approx(
        2.0 - row.storage_power_mw[0] * 0.95 / 60,
        abs=1e-6,
    )
    assert type(row).model_validate_json(row.model_dump_json()) == row


@pytest.mark.parametrize("fault", ["power", "resource", "clock", "missing_witness"])
def test_regional_certificate_cannot_be_detached_from_the_adopted_trajectory(fault):
    case, policy = case_and_policy(initial=-2.0)
    row, _ = realize_regional_plan(request(case, policy))
    raw = row.model_dump()
    if fault == "power":
        raw["storage_power_mw"] = (0.0,)
    elif fault == "resource":
        raw["resource_p_mw"][row.storage_reachability.inputs.resource_id] = (0.0,)
    elif fault == "clock":
        raw["time_minutes"] = (1.0,)
    else:
        raw["storage_reachability"] = None
    with pytest.raises(ValueError, match="storage reachability"):
        type(row).model_validate(raw)


@pytest.mark.parametrize(
    "fault", [None, "missing", "clock", "power", "resource_length"]
)
def test_rolling_prefix_summary_binds_every_window_witness(fault):
    case, policy = case_and_policy(initial=-2.0)
    row, _ = realize_regional_plan(request(case, policy))
    raw = row.model_dump()
    raw["storage_reachability_windows"] = [raw.pop("storage_reachability")]
    for check in raw["checks"]:
        if check["code"] == "STORAGE_REACHABILITY":
            check["code"] = "STORAGE_REACHABILITY_WINDOWS"
    if fault == "missing":
        raw["storage_reachability_windows"] = []
    elif fault == "clock":
        raw["time_minutes"] = (1.0,)
    elif fault == "power":
        raw["storage_power_mw"] = (0.0,)
    elif fault == "resource_length":
        raw["resource_p_mw"][row.storage_reachability.inputs.resource_id] = ()
    if fault:
        with pytest.raises(ValueError, match="rolling reachability"):
            type(row).model_validate(raw)
    else:
        summary = type(row).model_validate(raw)
        assert summary.executable_plan
        assert type(row).model_validate_json(summary.model_dump_json()) == summary


def test_legacy_policy_and_plan_do_not_acquire_a_reachability_certificate():
    case, policy = case_and_policy(enabled=False)
    row, _ = realize_regional_plan(request(case, policy))
    raw = row.model_dump()
    raw.pop("storage_reachability")
    assert type(row).model_validate(raw).storage_reachability is None
    assert ExecutionPolicy.model_validate({}).storage_reachability is None


def test_coordination_cache_rebuilds_when_actual_power_or_the_reachability_mode_changes():
    cache = CoordinationWorkspace()
    first, _ = case_and_policy(initial=0.0)
    changed, _ = case_and_policy(initial=-2.0)
    legacy = replace(changed, storage_reachability={})

    def prepare(case):
        return cache.prepare(
            case,
            case.microgrids,
            ADMMConfig(),
            DispatchCapabilities(),
            {"A": np.full(1, 0.15)},
            None,
        )[0]["A"]

    before = prepare(first)
    assert prepare(first) is before
    after = prepare(changed)
    assert after is not before
    with pytest.raises(ValueError, match="reachability"):
        before.update_window(changed, changed.microgrids[0], None, np.full(1, 0.15))
    assert prepare(legacy) is not after


@pytest.mark.parametrize("fault", ["missing", "stale", "energy", "unknown_region"])
def test_later_window_requires_complete_current_physical_state(fault):
    case, policy = case_and_policy(enabled=False)
    case = replace(case, time_hours=np.array([1.0 / 60]))
    state = DeviceCheckpoint(minute=1, storage_power_mw=2.5, storage_energy_mwh=2.0)
    states = {"A": state}
    if fault == "missing":
        states = {}
    elif fault == "stale":
        states = {"A": state.model_copy(update={"minute": 0})}
    elif fault == "energy":
        states = {"A": state.model_copy(update={"storage_energy_mwh": 1.9})}
    else:
        states["unknown"] = state
    with pytest.raises(ValueError, match="checkpoint"):
        with_storage_reachability(
            case,
            policy=StorageReachabilityPolicy(),
            dynamics=policy.dynamic_tracking,
            checkpoints=states,
            storage_enabled=True,
            execution_step_minutes=1,
        )


@pytest.mark.parametrize("minute", [31, 124])
def test_minute_hour_roundoff_does_not_reject_a_fresh_checkpoint_or_command(minute):
    case, policy = case_and_policy(initial=-2.0)
    case = replace(case, storage_reachability={}, time_hours=np.array([minute / 60]))
    assert (
        float(case.time_hours[0] * 60) != minute
    )  # Exercise the non-exact conversion.
    case = with_storage_reachability(
        case,
        policy=policy.storage_reachability,
        dynamics=policy.dynamic_tracking,
        checkpoints={
            "A": DeviceCheckpoint(
                minute=minute, storage_energy_mwh=2.0, storage_power_mw=-2.0
            )
        },
        storage_enabled=True,
        execution_step_minutes=1,
    )
    row, plan = realize_regional_plan(request(case, policy))
    assert plan is not None and row.executable_plan
    assert row.storage_reachability.inputs.start_minute == pytest.approx(minute)


def test_startup_assumption_is_explicit_and_disabled_storage_has_no_false_certificate():
    case, policy = case_and_policy(enabled=False)
    startup = with_storage_reachability(
        case,
        policy=StorageReachabilityPolicy(),
        dynamics=policy.dynamic_tracking,
        checkpoints={},
        storage_enabled=True,
        execution_step_minutes=1,
    )
    assert startup.storage_reachability["A"].initial_power_mw is None
    disabled = with_storage_reachability(
        startup,
        policy=StorageReachabilityPolicy(),
        dynamics=policy.dynamic_tracking,
        checkpoints={},
        storage_enabled=False,
        execution_step_minutes=1,
    )
    assert not disabled.storage_reachability


def test_nonuniform_time_grid_cannot_reuse_a_uniform_interval_certificate():
    case, _ = case_and_policy(initial=0.0, steps=2)
    with pytest.raises(ValueError, match="exact planning window"):
        replace(case, time_hours=np.array([0.0, 2.0 / 60]))


def test_new_policy_cannot_silently_omit_the_state_constraints():
    case, _ = case_and_policy(enabled=False)
    _, policy = case_and_policy()
    with pytest.raises(ValueError, match="reachability"):
        request(case, policy)

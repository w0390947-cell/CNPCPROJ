"""Continuous-session equivalence, prospective updates and retained prefixes."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.bootstrap.adapters.cluster_execution import _persistent_plant
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.device_control import (
    device_tracking_session,
    simulate_device_tracking,
)
from oilfield_energy.modules.control.contracts import (
    DevicePlanUpdate,
    StopDeviceSession,
)


@pytest.fixture(scope="module")
def inputs():
    case = build_synthetic_case(4)
    case = replace(
        case,
        time_hours=np.arange(4) / 4,
        assumptions=replace(case.assumptions, dt_hours=0.25),
    )
    mg = case.microgrids[0]
    checked = solve_case_ac_consistent(case, [mg.name], time_limit_seconds=20)
    assert checked.passed
    plant = _persistent_plant(case)[mg.name]
    base = simulate_device_tracking(case, mg, checked.optimization, plant_inputs=plant)
    return case, mg, checked.optimization, plant, base


def test_segment_boundaries_preserve_every_device_and_controller_state(inputs):
    case, mg, plan, plant, base = inputs
    session = device_tracking_session(case, mg, plan, plant_inputs=plant)
    current = next(session)
    while True:
        minute = current.minute
        command = None
        if minute % 15 == 0:
            schedules = plan.microgrids[mg.name]["resource_schedules"]
            command = DevicePlanUpdate(
                start_minute=minute,
                p_mw=tuple(base.pcc_command_mw[minute : minute + 15]),
                q_mvar=tuple(base.qcc_command_mvar[minute : minute + 15]),
                resource_p_mw={
                    r.resource_id: (float(r.active_power_mw[minute // 15]),) * 15 for r in schedules
                },
                resource_q_mvar={
                    r.resource_id: (float(r.reactive_power_mvar[minute // 15]),) * 15
                    for r in schedules
                },
            )
        if minute:
            assert current.storage_energy_mwh == base.storage_energy_mwh[minute - 1]
            assert current.storage_power_mw == base.storage_actual_mw[minute - 1]
        try:
            current = session.send(command)
        except StopIteration as done:
            result = done.value
            break
    for field in (
        "pcc_actual_mw",
        "qcc_actual_mvar",
        "storage_energy_mwh",
        "group_control_state",
        "network_recovery_blocked",
    ):
        np.testing.assert_array_equal(getattr(result, field), getattr(base, field))
    assert result.storage_dynamics == base.storage_dynamics


def test_abort_archives_only_executed_minutes(inputs):
    case, mg, plan, plant, base = inputs
    session = device_tracking_session(case, mg, plan, plant_inputs=plant)
    for _ in range(16):
        next(session)
    with pytest.raises(StopIteration) as stopped:
        session.throw(StopDeviceSession())
    result = stopped.value.value
    assert len(result.time_minutes) == 15
    np.testing.assert_array_equal(result.storage_energy_mwh, base.storage_energy_mwh[:15])
    np.testing.assert_array_equal(result.pcc_actual_mw, base.pcc_actual_mw[:15])
    assert len(result.storage_dynamics) == 15


def test_updates_cannot_rewrite_past_commands(inputs):
    case, mg, plan, plant, _ = inputs
    session = device_tracking_session(case, mg, plan, plant_inputs=plant)
    next(session)
    next(session)
    schedules = plan.microgrids[mg.name]["resource_schedules"]
    bad = DevicePlanUpdate(
        start_minute=0,
        p_mw=(1.0,),
        q_mvar=(0.0,),
        resource_p_mw={r.resource_id: (0.0,) for r in schedules},
        resource_q_mvar={r.resource_id: (0.0,) for r in schedules},
    )
    with pytest.raises(ValueError, match="current one-minute boundary"):
        session.send(bad)

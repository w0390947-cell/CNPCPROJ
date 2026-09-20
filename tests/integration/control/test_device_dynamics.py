"""Exercise public simulation paths, not only the standalone ramp projection."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.hierarchical import run_hierarchical_control
from oilfield_energy.hierarchy_reporting import write_minute_network_outputs
from oilfield_energy.hierarchy_types import TimeScaleConfig
from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs
from oilfield_energy.resource_control_contracts import ResourceType
from oilfield_energy.service import ScenarioType, SimulationRequest


@pytest.fixture(scope="module")
def solved_case():
    case = build_synthetic_case(steps=8)
    case = replace(
        case, time_hours=np.arange(8) * 0.25, assumptions=replace(case.assumptions, dt_hours=0.25)
    )
    mg = case.microgrids[0]
    checked = solve_case_ac_consistent(case, [mg.name], time_limit_seconds=60)
    assert checked.passed
    return case, mg, checked.optimization


def run_step(solved_case, *, direction=1, ramp=0.65, tau=2.0, emergency=False):
    case, mg, solved = solved_case
    target = np.zeros(8)
    target[2:] = direction
    data = dict(solved.microgrids[mg.name])
    data.update(
        p_grid_mw=np.full(8, 4 if direction < 0 else 2),
        q_grid_mvar=np.zeros(8),
        wind_available_mw=np.zeros(8),
    )
    data["resource_schedules"] = tuple(
        replace(
            s,
            active_power_mw=target.copy()
            if s.resource_type is ResourceType.STORAGE
            else np.zeros(8),
            reactive_power_mvar=np.zeros(8),
        )
        for s in data["resource_schedules"]
    )
    solved = replace(solved, microgrids={mg.name: data}, message="explicit synthetic response test")
    load = np.full(120, 4.0 if direction < 0 else 2.0)
    load[30:] = 2.0 if direction < 0 else 4.0
    if emergency:
        load[32:40] = 0.05

    def series(buses, active=False):
        return tuple(
            BusSeries(
                bus_id=b,
                values=tuple(
                    float(v) for v in (load if active and b == mg.storage.bus else np.zeros(120))
                ),
            )
            for b in buses
        )

    plant = PlantInputs(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        step_minutes=1,
        load_p=series(mg.buses, True),
        load_q=series(mg.buses),
        wind_available=series(mg.wind_available_mw),
        pv_available=series(mg.pv_available_mw),
    )
    tracking = simulate_device_tracking(
        case,
        mg,
        solved,
        plant_inputs=plant,
        config=TimeScaleConfig(
            active_power_ramp_mw_per_minute=ramp, device_time_constant_minutes=tau
        ),
        reverse_flow_probability_15=np.zeros(8),
    )
    return mg, tracking


def assert_energy_and_dynamics(mg, result):
    before = np.r_[mg.storage.e_initial_mwh, result.storage_energy_mwh[:-1]]
    p = result.storage_actual_mw
    expected = (
        before + np.where(p < 0, -p * mg.storage.eta_charge, -p / mg.storage.eta_discharge) / 60
    )
    np.testing.assert_allclose(result.storage_energy_mwh, expected, atol=1e-12, rtol=0)
    assert np.all(result.storage_energy_mwh >= mg.storage.e_min_mwh - 1e-12)
    assert np.all(result.storage_energy_mwh <= mg.storage.e_max_mwh + 1e-12)
    assert len(result.storage_dynamics) == len(p) == 120
    for k, record in enumerate(result.storage_dynamics):
        assert record.actual_mw == p[k]
        if k:
            assert record.previous_mw == p[k - 1]
        assert record.ramp_compliant or record.physical_override or record.hard_override
        assert record.ordinary_mw + record.ordinary_unserved_mw == pytest.approx(
            record.requested_mw
        )
        if not record.hard_override:
            assert record.actual_mw == record.ordinary_mw


@pytest.mark.parametrize(
    "direction,ramp,tau", [(1, 0.65, 2), (-1, 0.65, 2), (1, 0.1, 0.5), (-1, 0.3, 5)]
)
def test_schedule_and_pcc_correction_share_final_ramp_budget(solved_case, direction, ramp, tau):
    mg, result = run_step(solved_case, direction=direction, ramp=ramp, tau=tau)
    assert_energy_and_dynamics(mg, result)
    step = result.storage_dynamics[30]
    assert not step.physical_override and not step.hard_override
    assert abs(step.actual_mw - step.previous_mw) == pytest.approx(ramp)
    assert abs(step.ordinary_unserved_mw) > 0
    assert result.network_security_passed


def test_soc_conflict_is_reported_without_spending_nonexistent_energy(solved_case):
    mg, result = run_step(solved_case)
    assert_energy_and_dynamics(mg, result)
    conflicts = [r for r in result.storage_dynamics if r.physical_override]
    assert conflicts
    assert all(not r.ramp_compliant and not r.hard_override for r in conflicts)


def test_no_export_emergency_remains_explicit_and_energy_consistent(solved_case):
    mg, result = run_step(solved_case, emergency=True)
    assert_energy_and_dynamics(mg, result)
    assert any(r.hard_override and not r.ramp_compliant for r in result.storage_dynamics)
    assert result.no_reverse_violations_after_safety == 0
    assert result.network_security_passed


def test_export_preserves_overrides_unserved_power_and_legacy_unknown(solved_case, tmp_path):
    _, result = run_step(solved_case)
    legacy = replace(result, name="legacy", storage_dynamics=())
    write_minute_network_outputs(tmp_path, {result.name: result, "legacy": legacy})
    payload = json.loads((tmp_path / "storage_dynamics.json").read_text())
    assert payload[result.name]["version"] == "storage-dynamics-v1"
    assert payload[result.name]["complete"]
    assert payload[result.name]["records"][30]["ordinary_unserved_mw"] > 0
    assert any(
        r["physical_override"] and not r["ramp_compliant"] for r in payload[result.name]["records"]
    )
    assert payload["legacy"] == dict(version=None, complete=False, records=[])


def test_unrepresentable_grid_is_rejected_before_optimization_or_schedule_access():
    case = build_synthetic_case(steps=7)
    with pytest.raises(ValueError, match="exactly divide"):
        run_hierarchical_control(case, [mg.name for mg in case.microgrids])
    # A missing plan deliberately demonstrates that grid validation runs first.
    with pytest.raises(ValueError, match="exactly divide"):
        simulate_device_tracking(case, case.microgrids[0], None)


def test_planning_only_service_still_accepts_non_minute_grid():
    request = SimulationRequest(scenario_type=ScenarioType.CLUSTER_COORDINATION, steps=7)
    assert request.steps == 7


@pytest.mark.parametrize(
    "changes",
    [
        dict(device_step_minutes=1.5),
        dict(device_step_minutes=True),
        dict(device_step_minutes=0),
        dict(active_power_ramp_mw_per_minute=-1),
        dict(active_power_ramp_mw_per_minute=float("nan")),
        dict(device_time_constant_minutes=float("inf")),
    ],
)
def test_invalid_dynamic_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        TimeScaleConfig(**changes)

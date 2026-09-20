"""Integrated regressions: actual device state, AC evidence and restoration."""

import csv
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.group_control import GroupControlSupervisor
from oilfield_energy.hierarchy_reporting import (
    _group_control_metrics,
    _write_group_control_timeseries,
    write_minute_network_outputs,
)
from oilfield_energy.hierarchy_types import TimeScaleConfig
from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs
from oilfield_energy.resource_control_contracts import ResourceType


@pytest.fixture(scope="module")
def fixture_case():
    case = build_synthetic_case(steps=8)
    case = replace(
        case, time_hours=np.arange(8) * 0.25, assumptions=replace(case.assumptions, dt_hours=0.25)
    )
    mg = case.microgrids[0]
    checked = solve_case_ac_consistent(case, [mg.name], time_limit_seconds=60)
    assert checked.passed
    return case, mg, checked.optimization


def recovery_fixture(fixture_case, *, cloud=False):
    case, mg, solved = fixture_case
    mg = replace(mg, storage=replace(mg.storage, p_max_mw=1e-6))
    case = replace(case, microgrids=[mg])
    data = dict(solved.microgrids[mg.name])
    data.update(p_grid_mw=np.full(8, 0.2), q_grid_mvar=np.zeros(8), wind_available_mw=np.zeros(8))
    data["resource_schedules"] = tuple(
        replace(
            s,
            active_power_mw=np.full(8, 2.0 if s.resource_type is ResourceType.PV else 0.0),
            reactive_power_mvar=np.zeros(8),
        )
        for s in data["resource_schedules"]
    )
    solved = replace(solved, microgrids={mg.name: data}, message="synthetic control test targets")
    pv_bus = next(iter(mg.pv_available_mw))
    loads = np.full(120, 3.1)
    loads[:30] = 2.2
    available = np.full(120, 2.0)
    if cloud:
        available[32:34] = 1.0

    def series(buses, profile):
        return tuple(BusSeries(bus_id=b, values=tuple(float(v) for v in profile(b))) for b in buses)

    plant = PlantInputs(
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        step_minutes=1,
        load_p=series(mg.buses, lambda b: loads if b == pv_bus else np.zeros(120)),
        load_q=series(mg.buses, lambda b: np.zeros(120)),
        wind_available=series(mg.wind_available_mw, lambda b: np.zeros(120)),
        pv_available=series(mg.pv_available_mw, lambda b: available),
    )
    return case, mg, solved, plant


def test_default_plant_actual_totals_are_the_same_used_by_control_and_ac(fixture_case, monkeypatch):
    case, mg, solved = fixture_case
    observed = []
    original = GroupControlSupervisor.step

    def record(self, snapshot):
        observed.append(snapshot.pcc_power_mw)
        return original(self, snapshot)

    monkeypatch.setattr(GroupControlSupervisor, "step", record)
    result = simulate_device_tracking(case, mg, solved, reverse_flow_probability_15=np.zeros(8))
    assert np.all(result.wind_actual_mw <= result.wind_available_mw + 1e-9)
    assert np.all(result.pv_actual_mw <= result.pv_available_mw + 1e-9)
    assert np.max(result.wind_availability_limited_mw) > 1.0
    assert np.allclose(
        sum(result.wind_resource_actual_mw.values()), result.wind_actual_mw, atol=1e-9, rtol=0
    )
    pre = [
        r["flow"]["pcc_import_mw"] for r in result.network_feedback if r["stage"] == "pre_control"
    ]
    assert np.allclose(observed, pre, atol=1e-9, rtol=0)
    for k, record in enumerate(r for r in result.network_feedback if r["stage"] == "post_control"):
        wind = sum(
            d["p_mw"]
            for d in record["inputs"]["snapshot"]["devices"]
            if d["resource_type"] == "wind"
        )
        assert wind == pytest.approx(result.wind_actual_mw[k], abs=1e-9)
    assert result.network_security_passed


@pytest.mark.parametrize("tau, completes", [(20.0, False), (0.25, True)])
def test_recovery_verdict_depends_on_executed_response(fixture_case, tau, completes):
    case, mg, solved, plant = recovery_fixture(fixture_case)
    result = simulate_device_tracking(
        case,
        mg,
        solved,
        plant_inputs=plant,
        config=TimeScaleConfig(pv_device_time_constant_minutes=tau),
        reverse_flow_probability_15=np.ones(8),
    )
    evaluated = [e for e in result.group_restoration_evidence if e.status == "observed"]
    assert evaluated
    first = evaluated[0]
    # Reported time k is after control; PV response already executed before the
    # supervisor, and no emergency PV actuation occurs in this stable test.
    before = result.pv_actual_mw[int(first.issued_at_minutes)]
    after = result.pv_actual_mw[int(first.observed_at_minutes)]
    assert first.measured_delta_mw == pytest.approx(after - before, abs=1e-10)
    assert first.achieved_mw == max(0.0, first.measured_delta_mw)
    assert result.network_invalid_steps == result.network_violation_steps == 0
    if completes:
        assert len(evaluated) == 10
        assert np.count_nonzero(result.group_recovery_evaluation_passed == 1.0) == 10
        assert any(e.reason == "recovery_completed" for e in result.group_control_events)
    else:
        assert first.measured_delta_mw < 0.0
        assert result.group_recovery_evaluation_passed[34] == 0.0
        assert result.group_recovery_aborts == 1
        assert result.group_control_state[-1] == "recovery_inhibit"
        assert np.count_nonzero(result.group_recovery_evaluation_passed == 1.0) == 0


def test_weather_change_cannot_become_restoration_and_export_keeps_unknown(fixture_case, tmp_path):
    case, mg, solved, plant = recovery_fixture(fixture_case, cloud=True)
    result = simulate_device_tracking(
        case, mg, solved, plant_inputs=plant, reverse_flow_probability_15=np.ones(8)
    )
    unknown = [e for e in result.group_restoration_evidence if e.status == "unknown"]
    assert unknown and unknown[0].reason == "restoration_exogenous_context_changed"
    assert result.group_control_state[-1] == "recovery_inhibit"
    assert np.count_nonzero(result.group_recovery_evaluation_passed == 1.0) == 0
    assert result.group_remaining_curtailment_mw[-1] > 0.1
    write_minute_network_outputs(tmp_path, {mg.name: result})
    raw = (tmp_path / "minute_execution.json").read_text(encoding="utf-8")
    assert "NaN" not in raw and "Infinity" not in raw
    exported = json.loads(raw)[mg.name]
    assert exported["version"] == "minute-execution-v2"
    assert any(
        e["status"] == "unknown" and e["achieved_mw"] is None
        for e in exported["restoration_evidence"]
    )
    assert _group_control_metrics(result)["achieved_restoration_mw_sum"] is None
    historical = replace(
        result, execution_evidence_version=None, group_achieved_restoration_mw=np.ones(120)
    )
    _write_group_control_timeseries(
        tmp_path / "historical.csv", SimpleNamespace(tracking={mg.name: historical})
    )
    with (tmp_path / "historical.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert all(
        r["achieved_restoration_mw"] == "" and r["execution_evidence_version"] == "" for r in rows
    )

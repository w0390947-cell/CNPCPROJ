"""Real device/network execution with exhausted storage and renewable headroom."""

from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.actuation_arbiter import ActuationArbiter
from oilfield_energy.bootstrap.adapters.cluster_rolling import window_case
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.control_contracts import CapIntentAction, StationCapIntent
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs


@pytest.fixture(scope="module")
def tracking_inputs():
    base = build_synthetic_case(96, profile_kind="intraday")
    energies = {m.name: m.storage.e_min_mwh for m in base.microgrids}
    case = window_case(base, 35, 36, energies, energies)
    mg = case.microgrids[0]
    solved = solve_case_ac_consistent(
        case, [mg.name], storage_enabled=False, cluster_coordination=False
    )
    assert solved.passed
    groups = {}
    for field, source in (
        ("load_p", "load_p_mw"),
        ("load_q", "load_q_mvar"),
        ("wind_available", "wind_available_mw"),
        ("pv_available", "pv_available_mw"),
    ):
        groups[field] = tuple(
            BusSeries(bus_id=b, values=(float(v[0]),) * 15)
            for b, v in getattr(mg, source).items()
        )
    # Small unexpected demand; actual sunlight exceeds the previously adopted plan.
    load = list(groups["load_p"])
    load[0] = load[0].model_copy(
        update={"values": tuple(v + 0.12 for v in load[0].values)}
    )
    groups["load_p"] = tuple(load)
    pv = list(groups["pv_available"])
    pv[0] = pv[0].model_copy(update={"values": tuple(v + 0.3 for v in pv[0].values)})
    groups["pv_available"] = tuple(pv)
    plant = PlantInputs(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc), step_minutes=1, **groups
    )
    return case, mg, solved.optimization, plant


def run(inputs, *, storage_enabled=False, **kwargs):
    case, mg, plan, plant = inputs
    return simulate_device_tracking(
        case, mg, plan, plant_inputs=plant, storage_enabled=storage_enabled, **kwargs
    )


@pytest.mark.parametrize("storage_enabled", [False, True])
def test_pv_headroom_serves_tracking_when_storage_is_unavailable(
    tracking_inputs, storage_enabled
):
    result = run(tracking_inputs, storage_enabled=storage_enabled)
    assert np.max(np.abs(result.pcc_actual_mw[3:] - result.pcc_command_mw[3:])) < 0.02
    if not storage_enabled:
        assert np.max(np.abs(result.storage_actual_mw)) == 0
        assert np.ptp(result.storage_energy_mwh) == 0
    assert np.min(result.storage_energy_mwh) >= tracking_inputs[1].storage.e_min_mwh
    adopted = [
        r
        for r in result.network_feedback
        if r.get("active_tracking", {}).get("status") == "adopted"
    ]
    assert adopted
    assert result.network_security_passed


def test_invalid_candidate_cannot_be_adopted_or_reported_as_execution(tracking_inputs):
    def reject(snapshot, stage):
        return (
            replace(snapshot, quality_valid=False)
            if stage == "active_tracking_candidate"
            else snapshot
        )

    result = run(tracking_inputs, network_snapshot_adapter=reject)
    assert np.min(result.pcc_actual_mw[3:] - result.pcc_command_mw[3:]) > 0.08
    probes = [
        r for r in result.network_feedback if r["stage"] == "active_tracking_candidate"
    ]
    assert probes and all(not r["is_actual"] for r in probes)
    assert not any(
        r.get("active_tracking", {}).get("status") == "adopted"
        for r in result.network_feedback
    )


def test_owned_cap_is_never_released_to_use_sunlight_headroom(
    tracking_inputs, monkeypatch
):
    original = ActuationArbiter.apply
    observed = []

    def cap(self, available, intents=()):
        owned = tuple(
            StationCapIntent(
                owner_id="external_dispatch",
                station_id=sid,
                action=CapIntentAction.SET_CAP,
                absolute_cap_mw=value,
                decision_id="external-cap",
            )
            for sid, value in available.items()
            if self.owner_cap("external_dispatch", sid) is None
        )
        result = original(self, available, (*intents, *owned))
        observed.append(self.owner_caps("external_dispatch"))
        return result

    monkeypatch.setattr(ActuationArbiter, "apply", cap)
    result = run(tracking_inputs)
    assert np.min(result.pcc_actual_mw[3:] - result.pcc_command_mw[3:]) > 0.08
    assert observed and all(row == observed[0] for row in observed)


def test_wind_headroom_is_used_when_no_pv_headroom_exists(tracking_inputs):
    case, mg, plan, plant = tracking_inputs
    pv = tuple(
        BusSeries(bus_id=b, values=(float(v[0]),) * 15)
        for b, v in mg.pv_available_mw.items()
    )
    wind = list(plant.wind_available)
    wind[0] = wind[0].model_copy(
        update={"values": tuple(v + 0.4 for v in wind[0].values)}
    )
    changed = plant.model_copy(
        update={"pv_available": pv, "wind_available": tuple(wind)}
    )
    result = run((case, mg, plan, changed))
    assert np.max(np.abs(result.pcc_actual_mw[5:] - result.pcc_command_mw[5:])) < 0.02
    assert result.network_security_passed


def test_downward_tracking_when_storage_cannot_absorb(tracking_inputs):
    case, mg, plan, plant = tracking_inputs
    load = list(plant.load_p)
    load[0] = load[0].model_copy(
        update={"values": tuple(v - 0.24 for v in load[0].values)}
    )
    changed = plant.model_copy(update={"load_p": tuple(load)})
    result = run((case, mg, plan, changed))
    assert np.max(np.abs(result.pcc_actual_mw[3:] - result.pcc_command_mw[3:])) < 0.02
    assert result.network_security_passed


def test_no_headroom_preserves_real_unserved_error(tracking_inputs):
    case, mg, plan, plant = tracking_inputs
    pv = tuple(
        BusSeries(bus_id=b, values=(float(v[0]),) * 15)
        for b, v in mg.pv_available_mw.items()
    )
    result = run((case, mg, plan, plant.model_copy(update={"pv_available": pv})))
    assert np.min(result.pcc_actual_mw[3:] - result.pcc_command_mw[3:]) > 0.08
    records = [
        r["active_tracking"] for r in result.network_feedback if "active_tracking" in r
    ]
    assert any(
        r["status"] == "capacity_limited" and r["proposal"]["unserved_change_mw"] > 0.08
        for r in records
    )

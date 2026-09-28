"""Source-derived Yan'an topology and units, separate from simulated assumptions."""

from dataclasses import replace
from math import sqrt

import pytest

from oilfield_energy.bootstrap.adapters.demo_case import build_bundle, case_from_bundle
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case, study_recipe
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkOperatingMode,
    ShuntOperatingPoint,
    assess_network_model,
)


@pytest.mark.parametrize("region,index", [("YA_B", 1), ("YA_C", 2)])
def test_five_mw_wind_and_declared_synthetic_extensions_survive_capture(region, index):
    # XJTU pp1-3: 化140-265 and 山8增 are each ONE 5 MW turbine.
    bundle = build_bundle()
    spec = study_recipe(region)
    for case in (build_synthetic_case(8), case_from_bundle(bundle, 8)):
        mg = case.microgrids[index]
        assert mg.wind_capacity_mw == {f"{region}_WIND": 5.0}
        assert mg.wind_capacity_mva == {f"{region}_WIND": 5.25}
        assert mg.wind_q_abs_max_mvar == {f"{region}_WIND": 1.64}
        assert mg.wind_q_over_p_limit(f"{region}_WIND") == 0.333
        assert not mg.storage_reactive_enabled
        assert not mg.pv_can_control_reactive(f"{region}_PV")
        network = mg.network_model_v2
        assert assess_network_model(network).ready_for_current_solver
        assert not assess_network_model(network).ready_for_field_case
        buses = {b.bus_id: b for b in network.buses}
        assert buses[f"{region}_MAIN"].nominal_voltage_kv == 35.0
        assert buses[f"{region}_WIND"].nominal_voltage_kv == 35.0
        assert buses[f"{region}_LV"].nominal_voltage_kv == 10.0
        assert all(b.provenance for b in network.buses)
        assert all(b.provenance for b in network.branches)
    for resource in spec.resources:
        assert resource.capacity_provenance
        if resource.kind != "wind":
            assert "Synthetic research extension" in resource.capacity_provenance
        if resource.kind in ("pv", "storage"):
            assert resource.q_max_mvar == 0.0


def test_huaziping_t_connection_retains_both_halves_and_documented_amp_limit():
    mg = build_synthetic_case(4).microgrids[1]
    branches = {b.branch_id: b for b in mg.network_model_v2.branches}
    near, far = (branches[f"YA_B_HUALIAN_{part}"] for part in ("NEAR", "FAR"))
    assert (near.from_bus_id, near.to_bus_id) == ("YA_B_MAIN", "YA_B_WIND")
    assert (far.from_bus_id, far.to_bus_id) == ("YA_B_WIND", "YA_B_REMOTE")
    # XJTU p9: 14.72 km electrical line, not the 7 km communications fiber (p1).
    z_base = 35.0**2 / 20.0
    assert (near.r_pu + far.r_pu) * z_base == pytest.approx(0.32 * 14.72)
    assert (near.x_pu + far.x_pu) * z_base == pytest.approx(0.40 * 14.72)
    assert near.s_max_mva == pytest.approx(sqrt(3) * 35 * 78 / 1000)
    assert far.s_max_mva == near.s_max_mva
    assert not mg.load_p_mw["YA_B_WIND"].any()
    assert mg.load_p_mw["YA_B_REMOTE"].min() > 0


@pytest.mark.parametrize("index,km,amps", [(1, 7.03, 270), (2, 16.88, 272)])
def test_supply_uses_electrical_length_and_nominal_voltage_current_conversion(index, km, amps):
    mg = build_synthetic_case(4).microgrids[index]
    supply = next(b for b in mg.network_model_v2.branches if b.from_bus_id == mg.pcc_bus)
    assert supply.r_pu == pytest.approx(0.32 * km * 20 / 35**2)
    assert supply.x_pu == pytest.approx(0.40 * km * 20 / 35**2)
    assert supply.s_max_mva == pytest.approx(sqrt(3) * 35 * amps / 1000)


@pytest.mark.parametrize(
    "index,expected_r,expected_x,capacity",
    [
        (1, 0.0108215768685, 0.1027649983490, 13.97421080136),
        (2, 0.0088888888889, 0.0676854890807, 22.5),
    ],
)
def test_parallel_transformer_equivalent_uses_own_rating_and_10_5kv_secondary(
    index, expected_r, expected_x, capacity
):
    mg = build_synthetic_case(4).microgrids[index]
    transformer = next(
        b for b in mg.network_model_v2.branches if b.kind is NetworkBranchKind.TRANSFORMER
    )
    # Independently calculated from XJTU pp8-9; neutral taps are a study assumption.
    assert transformer.r_pu == pytest.approx(expected_r, abs=1e-12)
    assert transformer.x_pu == pytest.approx(expected_x, abs=1e-12)
    assert transformer.s_max_mva == pytest.approx(capacity)
    assert transformer.fixed_tap_ratio == pytest.approx(10 / 10.5)
    assert transformer.from_bus_id == f"{mg.name}_MAIN"
    assert transformer.to_bus_id == f"{mg.name}_LV"
    assert transformer.tap_changer is None  # Not a fictitious common on-load tap controller.


@pytest.mark.parametrize("index,ratings", [(1, [0.9, 0.9, 0.8, 1.6]), (2, [2.4])])
def test_documented_capacitors_are_recorded_but_not_assumed_energized(index, ratings):
    mg = build_synthetic_case(4).microgrids[index]
    network = mg.network_model_v2
    assert [s.q_per_step_mvar for s in network.shunts] == ratings
    assert all(s.default_steps == 0 for s in network.shunts)
    resolved = assess_network_model(network).require_current_solver_ready()
    assert sum(resolved.shunt_q_nominal_mvar_by_bus.values()) == 0.0
    energized = replace(
        network,
        operating_modes=(
            NetworkOperatingMode(
                "normal",
                "explicit synthetic capacitor sensitivity case",
                shunt_operating_points=tuple(
                    ShuntOperatingPoint(s.shunt_id, True, 1) for s in network.shunts
                ),
            ),
        ),
    )
    nominal = (
        assess_network_model(energized).require_current_solver_ready().shunt_q_nominal_mvar_by_bus
    )
    assert nominal[f"{mg.name}_LV"] == pytest.approx(sum(ratings))

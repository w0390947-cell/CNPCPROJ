"""Documented SC boundaries survive network, optimization and device projections."""

from dataclasses import replace

import pytest

from oilfield_energy.bootstrap.adapters.demo_case import build_bundle, case_from_bundle
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkOperatingMode,
    SwitchState,
    assess_network_model,
)


def test_35kv_connection_has_separate_storage_svg_and_wind_feeders():
    # Independent source: control PDF p1 single-line diagram, XJTU PDF p9 SVG entry.
    mg = build_synthetic_case(4).microgrids[0]
    network = mg.network_model_v2
    assert network is not None
    assert assess_network_model(network).ready_for_current_solver
    assert not assess_network_model(network).ready_for_field_case
    buses = {bus.bus_id: bus for bus in network.buses}
    incoming = {branch.to_bus_id: branch for branch in network.branches}
    connection = incoming["SC_MAIN"]
    assert connection.from_bus_id == mg.pcc_bus
    assert connection.kind is NetworkBranchKind.LINE
    assert buses[connection.from_bus_id].nominal_voltage_kv == 35.0
    assert buses[connection.to_bus_id].nominal_voltage_kv == 35.0
    assert mg.svg_bus != mg.storage.bus
    for bus_id in (mg.svg_bus, mg.storage.bus, "SC_WIND_COLLECTOR"):
        assert incoming[bus_id].from_bus_id == "SC_MAIN"
        assert buses[bus_id].nominal_voltage_kv == 35.0
    assert {incoming[bus].from_bus_id for bus in mg.wind_capacity_mw} == {"SC_WIND_COLLECTOR"}
    # PV remains a declared synthetic extension, not an invented field connection.
    for bus_id in mg.pv_capacity_mw:
        assert buses[bus_id].nominal_voltage_kv == 10.0
        assert buses[bus_id].provenance
    assert all(branch.provenance for branch in network.branches)


@pytest.mark.parametrize(
    "switch_id", ["SC_GRID_DISCONNECT", "SC_PCC_SWITCH", "SC_WIND_TRIP_SWITCH"]
)
def test_documented_switch_opening_is_not_silently_solved_as_connected(switch_id):
    network = build_synthetic_case(4).microgrids[0].network_model_v2
    opened = replace(
        network,
        operating_modes=(
            NetworkOperatingMode(
                "normal",
                "deliberate disconnection",
                switch_states=(SwitchState(switch_id, False),),
            ),
        ),
    )
    assessment = assess_network_model(opened)
    assert not assessment.ready_for_current_solver
    assert {"SC_WT1", "SC_WT2"} <= set(assessment.disconnected_bus_ids)


def test_svg_rating_and_dispatch_limit_remain_distinct_in_demo_round_trip():
    bundle = build_bundle()
    for case in (build_synthetic_case(4), case_from_bundle(bundle, 4)):
        sc = case.microgrids[0]
        capability = sc.svg_capability()
        assert capability.s_max_mva == 2.0
        assert capability.effective_q_min_mvar == -1.8
        assert capability.effective_q_max_mvar == 1.8
        device = next(d for d in bundle.devices if d.region == "SC" and d.kind == "svg")
        assert device.s_max_mva == 2.0
        assert device.q_max_mvar == 1.8
        assert device.bus_id == sc.svg_bus
        # Existing equipment identity survives the split from the storage bus.
        assert device.device_id == "SC:svg:SC_FLEX"

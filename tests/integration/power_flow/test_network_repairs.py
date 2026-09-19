"""Public AC/scenario integration: no substituted solutions or hidden bad snapshots."""

import csv
import json
from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkBus,
    NetworkContingency,
    NetworkDataProvenance,
    NetworkModelV2,
    NetworkOperatingMode,
    NetworkPhaseModel,
    SeriesBranch,
    assess_network_model,
)
from oilfield_energy.network_scenarios import (
    NetworkOperatingPoint,
    NetworkScenario,
    NetworkSecurityLimits,
    ScenarioStatus,
    build_network_scenarios,
    evaluate_network_scenarios,
    write_network_scenario_outputs,
)


@pytest.fixture
def model():
    return NetworkModelV2(
        network_id="repair-test",
        base_mva=10.0,
        pcc_bus_id="PCC",
        buses=(NetworkBus("PCC", "PCC", 10.0), NetworkBus("A", "A", 10.0)),
        branches=(SeriesBranch("L", "PCC", "A", NetworkBranchKind.LINE, 0.1, 0, 200),),
        operating_modes=(NetworkOperatingMode("normal", "synthetic"),),
        default_operating_mode_id="normal",
        provenance=NetworkDataProvenance("synthetic repair fixture", "1", synthetic=True),
        contingencies=(NetworkContingency("lose-L", ("L",)),),
    )


@pytest.fixture
def point():
    return NetworkOperatingPoint(
        "p", "synthetic", {"PCC": 0.0, "A": 1.0}, {"PCC": 0.0, "A": 0.0}, True, True
    )


@pytest.fixture
def limits():
    return NetworkSecurityLimits(0.95, 1.05, 0.0, 200.0, 0.0)


@pytest.mark.parametrize("load", [100.0, 100.0 - 1e-7, 100.0 + 1e-7])
def test_singular_voltage_is_not_converged_and_public_result_has_no_physical_values(
    model, point, limits, load
):
    point = replace(point, p_demand_mw_by_bus={"PCC": 0.0, "A": load})
    flow = backward_forward_sweep_resolved(
        assess_network_model(model).require_current_solver_ready(),
        np.array([0.0, load]),
        np.zeros(2),
    )
    assert not flow["converged"] and not flow["physical_values_valid"]
    assert flow["stop_reason"] == "SINGULAR_CONSTANT_POWER_VOLTAGE"
    result = evaluate_network_scenarios(
        model, (point,), (NetworkScenario("base", "normal"),), limits
    )
    sample = result.results[0].stages[0].points[0]
    assert sample.status is ScenarioStatus.NOT_CONVERGED
    assert sample.pcc_import_mw is None and not sample.buses and not sample.branches
    assert not result.all_final_states_secure


def test_loose_voltage_step_does_not_bypass_original_power_residual(model):
    network = assess_network_model(model).require_current_solver_ready()
    args = network, np.array([0.0, 10.0]), np.zeros(2)
    rejected = backward_forward_sweep_resolved(*args, tolerance=1.0, max_iterations=1)
    assert not rejected["converged"]
    assert rejected["stop_reason"] == "POWER_BALANCE_RESIDUAL"
    assert rejected["maximum_power_balance_residual_pu"] == pytest.approx(0.1)
    accepted = backward_forward_sweep_resolved(*args, tolerance=1.0, power_tolerance_pu=1e-10)
    expected = (1 + np.sqrt(0.6)) / 2
    assert accepted["converged"]
    assert accepted["voltage_pu"][1] == pytest.approx(expected, abs=1e-10)
    assert accepted["maximum_power_balance_residual_pu"] <= 1e-10


@pytest.mark.parametrize("kind", ["quality", "coherence", "nan", "missing"])
@pytest.mark.parametrize("topology", ["radial", "island", "unsupported"])
def test_bad_snapshots_remain_visible_across_topologies(model, point, limits, kind, topology):
    bad = replace(
        point,
        point_id="bad",
        **{
            "quality": {"quality_valid": False},
            "coherence": {"coherent": False},
            "nan": {"p_demand_mw_by_bus": {"PCC": 0.0, "A": float("nan")}},
            "missing": {"p_demand_mw_by_bus": {"PCC": 0.0}},
        }[kind],
    )
    if topology == "unsupported":
        model = replace(model, phase_model=NetworkPhaseModel.THREE_PHASE_UNBALANCED)
    scenarios = (
        build_network_scenarios(model)
        if topology == "island"
        else (NetworkScenario("base", "normal"),)
    )
    result = evaluate_network_scenarios(model, (point, bad), scenarios, limits)
    assert result.inputs_valid is False and not result.n_minus_one_coverage_complete
    assert result.point_inputs[0].valid and not result.point_inputs[1].valid
    assert not result.all_final_states_secure
    stage = result.results[-1].stages[0]
    assert stage.status is ScenarioStatus.INVALID_INPUT
    assert any(p.point_id == "bad" and p.reasons for p in stage.points)
    if topology == "island":
        assert stage.topology_status is ScenarioStatus.ISLANDED
        assert stage.disconnected_bus_ids == ("A",)
    elif topology == "unsupported":
        assert stage.topology_status is ScenarioStatus.UNSUPPORTED
    else:
        assert stage.points[0].status is ScenarioStatus.SECURE


def test_valid_island_still_counts_as_checked_and_historical_input_is_unknown(model, point, limits):
    batch = evaluate_network_scenarios(model, (point,), build_network_scenarios(model), limits)
    assert batch.inputs_valid is True and batch.n_minus_one_coverage_complete
    assert not batch.all_final_states_secure
    assert batch.results[-1].status is ScenarioStatus.ISLANDED
    historical = replace(batch, point_inputs=())
    assert historical.inputs_valid is None and not historical.n_minus_one_coverage_complete


def test_bad_input_cannot_be_hidden_by_recovery_or_nonapplicable_scenario(model, point, limits):
    bad = replace(point, quality_valid=False)
    scenario = NetworkScenario("fault", "normal", "lose-L", "normal")
    batch = evaluate_network_scenarios(model, (bad,), (scenario,), limits)
    assert len(batch.results[0].stages) == 1
    assert batch.results[0].status is ScenarioStatus.INVALID_INPUT
    offline = replace(
        model,
        branches=(
            *model.branches,
            replace(model.branches[0], branch_id="OFF", normally_in_service=False),
        ),
        contingencies=(*model.contingencies, NetworkContingency("lose-off", ("OFF",))),
    )
    # Not-applicable faults also retain independent bad-input evidence.
    batch = evaluate_network_scenarios(
        offline, (bad,), (NetworkScenario("fault", "normal", "lose-off"),), limits
    )
    assert batch.results[0].status is ScenarioStatus.NOT_APPLICABLE
    assert batch.inputs_valid is False
    assert batch.point_inputs[0].reasons == ("POINT_QUALITY_INVALID",)


def test_failure_does_not_erase_other_points_or_export_topology(model, point, limits, tmp_path):
    collapsed = replace(point, point_id="collapse", p_demand_mw_by_bus={"PCC": 0.0, "A": 100.0})
    bad = replace(point, point_id="bad", quality_valid=False)
    batch = evaluate_network_scenarios(
        model, (point, collapsed, bad), build_network_scenarios(model), limits
    )
    samples = batch.results[0].stages[0].points
    assert [s.status for s in samples] == [
        ScenarioStatus.SECURE,
        ScenarioStatus.NOT_CONVERGED,
        ScenarioStatus.INVALID_INPUT,
    ]
    write_network_scenario_outputs(tmp_path, batch, model=model, points=(point, collapsed, bad))
    report = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == "network-scenarios-v2"
    assert report["inputs_valid"] is False and not report["n_minus_one_coverage_complete"]
    assert report["results"][0]["stages"][0]["points"][1]["pcc_import_mw"] is None
    with (tmp_path / "points.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert any(r["status"] == "islanded" and r["point_id"] == "" for r in rows)
    assert any(r["status"] == "invalid_input" and r["point_id"] == "bad" for r in rows)

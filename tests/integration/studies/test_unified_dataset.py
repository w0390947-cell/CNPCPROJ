"""Business entries must share one authority, independently of legacy fixtures."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.demo_case import (
    DemoNetwork,
    build_bundle,
    case_from_bundle,
    decode_bundle,
    encode,
    plain,
)
from oilfield_energy.bootstrap.adapters.project_dataset import (
    build_synthetic_case,
    load_dataset,
    study_recipe,
)
from oilfield_energy.bootstrap.adapters.simulation_case import make_case
from oilfield_energy.bootstrap.adapters.simulation_files import load_study
from oilfield_energy.bootstrap.shancheng_simulation import generate_simulation
from oilfield_energy.modules.measurements.adapters.local_files import LocalDatasetReader
from oilfield_energy.modules.measurements.adapters.manifest_json import JsonManifestDecoder
from oilfield_energy.modules.measurements.api import CaptureDataset
from oilfield_energy.modules.studies.api import generate_inputs
from oilfield_energy.modules.studies.contracts import UnifiedDataset
from oilfield_energy.service import ScenarioType, SimulationRequest, run_simulation
from tests.legacy_case_fixture import build_synthetic_case as legacy_case


def test_web_cli_demo_and_study_share_ratings_topology_and_day_ahead_profiles(tmp_path):
    direct = build_synthetic_case()
    bundle = build_bundle()
    demo = case_from_bundle(bundle, 96)
    assert [len(m.buses) for m in direct.microgrids] == [10, 7, 7]
    assert (direct.dataset_id, direct.dataset_revision, direct.dataset_sha256) == (
        demo.dataset_id,
        demo.dataset_revision,
        demo.dataset_sha256,
    )
    for reference, captured in zip(direct.microgrids, demo.microgrids, strict=True):
        assert reference.resource_identities == captured.resource_identities
        assert reference.lines == captured.lines
        assert reference.storage == captured.storage
        assert reference.network_model_v2.buses == captured.network_model_v2.buses
        np.testing.assert_array_equal(
            reference.load_p_mw[reference.buses[1]], captured.load_p_mw[reference.buses[1]]
        )
        spec = study_recipe(reference.name)
        projected = make_case(spec, reference.network_model_v2, generate_inputs(spec)[0])
        assert plain(projected.microgrids[0]) == plain(reference)
        np.testing.assert_array_equal(projected.price_cny_per_mwh, direct.price_cny_per_mwh)
    generate_simulation(None, tmp_path / "field")
    captured = CaptureDataset(LocalDatasetReader(), JsonManifestDecoder()).capture(
        tmp_path / "field/manifest.json"
    )
    spec, day, intraday, plant = load_study(captured)
    assert spec == study_recipe()
    assert (day, intraday, plant) == generate_inputs(spec)
    assert day != intraday
    network = DemoNetwork(bundle)
    device = next(d for d in bundle.devices if d.device_id == "SC:wind:SC_WT1")
    assert network.available(device, 37) == next(
        s.values[37] for s in plant.wind_available if s.bus_id == device.bus_id
    )


@pytest.mark.parametrize("steps", [1, 7, 8, 24, 96, 288])
def test_resampling_is_shared_and_does_not_mutate_source(steps):
    case = build_synthetic_case(steps)
    decoded = case_from_bundle(build_bundle(), steps)
    assert plain(replace(case, microgrids=[])) == plain(replace(decoded, microgrids=[]))
    for a, b in zip(case.microgrids, decoded.microgrids, strict=True):
        for name in ("load_p_mw", "load_q_mvar", "wind_available_mw", "pv_available_mw"):
            assert plain(getattr(a, name)) == plain(getattr(b, name))
    case.microgrids[0].load_p_mw["SC_MAIN"][0] = -99
    assert build_synthetic_case(steps).microgrids[0].load_p_mw["SC_MAIN"][0] > 0


def test_historical_case_keeps_original_identity_and_topology():
    old = json.loads(encode(legacy_case()))
    for key in ("dataset_id", "dataset_revision", "dataset_sha256", "profile_kind"):
        old.pop(key)
    # Decoder accepts a captured legacy representation without claiming it is new.
    bundle = build_bundle().model_copy(
        update={"case": old, "dataset_id": "historical-fixture", "plant_profiles": None}
    )
    result = case_from_bundle(bundle, 8)
    assert result.dataset_id is None
    assert len(result.microgrids[0].buses) == 5
    assert result.microgrids[0].wind_capacity_mw == {"SC_WIND": 10.0}


def test_identity_mismatch_and_missing_plant_region_fail_closed():
    dataset, _ = load_dataset()
    raw = json.loads(dataset.model_dump_json())
    raw["regions"][0]["revision"] = "different"
    with pytest.raises(ValueError, match="identity mismatch"):
        UnifiedDataset.model_validate_json(json.dumps(raw), strict=True)
    bundle = json.loads(build_bundle().model_dump_json())
    del bundle["plant_profiles"]["SC"]
    with pytest.raises(ValueError, match="plant region coverage"):
        decode_bundle(encode(bundle))


@pytest.mark.parametrize("region", ["SC", "YA_B", "YA_C"])
def test_real_single_microgrid_solver_accepts_authoritative_topology(region):
    result = run_simulation(SimulationRequest(region=region, steps=8))
    assert result.metadata.dataset_id == load_dataset()[0].dataset_id
    assert result.executive_summary.overall_passed
    assert len(result.topology_nodes) == (10 if region == "SC" else 7)


def test_real_cluster_and_group_use_unified_inputs():
    for scenario in (ScenarioType.CLUSTER_COORDINATION, ScenarioType.GROUP_CONTROL):
        result = run_simulation(SimulationRequest(scenario_type=scenario, steps=8))
        assert result.metadata.dataset_sha256 == load_dataset()[1]
        assert result.executive_summary.overall_passed == all(i.passed for i in result.validation_items)
        if scenario is ScenarioType.CLUSTER_COORDINATION:
            assert len(result.cluster_execution.stages) == 3
            assert result.cluster_execution.dataset_sha256 == load_dataset()[1]


def test_generated_web_facts_match_authority():
    root = Path(__file__).resolve().parents[3]
    facts = json.loads(
        (root / "web/frontend/shared/api/generated/dataset-facts.json").read_text(encoding="utf-8")
    )
    assert facts["sha256"] == load_dataset()[1]
    assert facts["regions"]["SC"]["loadScaleMw"] == 14.0

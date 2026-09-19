import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.simulation_files import load_study, validate_json
from oilfield_energy.bootstrap.shancheng_simulation import create_simulation, generate_simulation
from oilfield_energy.modules.measurements.adapters.local_files import LocalDatasetReader
from oilfield_energy.modules.measurements.adapters.manifest_json import JsonManifestDecoder
from oilfield_energy.modules.measurements.api import CaptureDataset

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    folder = tmp_path_factory.mktemp("SC-complete")
    recipe = read(ROOT / "examples/shancheng_simulation/recipe.json")
    recipe.update(intervals=4, start="2026-09-15T12:00:00+08:00", rolling_horizon_intervals=4)
    config = folder / "recipe.json"
    config.write_text(json.dumps(recipe), encoding="utf-8")
    dataset = folder / "inputs"
    generate_simulation(config, dataset)
    with patch(
        "oilfield_energy.data.build_synthetic_case", side_effect=AssertionError("fallback called")
    ):
        result = create_simulation().execute(dataset / "manifest.json", folder / "results")
    assert result.exit_code == 0, read(result.output / "study_summary.json")
    return dataset, result.output, config


def test_complete_chain_and_input_source(run):
    dataset, output, _ = run
    report = read(output / "study_summary.json")
    assert report["passed"] and len(report["checks"]) >= 40
    assert all(report["checks"].values())
    assert len(list((output / "intraday").glob("*.json"))) == 4
    tracking = read(output / "minute_normal/tracking.json")
    assert len(tracking["time_minutes"]) == 60
    plant = read(dataset / "plant.json")
    lines = (output / "minute_normal/minute_network.jsonl").read_text(encoding="utf-8").splitlines()
    snapshot = json.loads(lines[0])["inputs"]["snapshot"]
    expected = {s["bus_id"]: s["values"][0] for s in plant["load_p"]}
    assert {s["bus_id"]: s["p_mw"] for s in snapshot["loads"]} == expected
    assert snapshot["at"] == plant["start"]


def test_actual_trip_unknowns_and_energy_conservation(run):
    _, output, _ = run
    trip = read(output / "faults/wind_trip/tracking.json")
    assert trip["wind_actual_mw"][10:15] == [0.0] * 5
    bad = read(output / "faults/bad_quality/tracking.json")
    assert bad["pcc_actual_mw"][10:15] == [None] * 5
    assert all(bad["network_recovery_blocked"][10:20])
    assert not bad["network_recovery_blocked"][20]
    tracking = read(output / "minute_normal/tracking.json")
    energy = np.array(tracking["storage_energy_mwh"])
    power = np.array(tracking["storage_actual_mw"])
    before = np.r_[2.5, energy[:-1]]
    assert (
        np.max(
            abs(
                energy
                - before
                + np.maximum(power, 0) / 0.95 / 60
                - np.maximum(-power, 0) * 0.95 / 60
            )
        )
        < 1e-9
    )


def test_fixed_comparison_keeps_pq_conditions(run):
    _, output, _ = run
    for path in (output / "comparisons").glob("*.json"):
        report = read(path)
        points = report["operating_points"]
        assert sum(points[1]["p_demand_mw_by_bus"].values()) - sum(
            points[0]["p_demand_mw_by_bus"].values()
        ) == pytest.approx(-0.5 if report["perturbation"]["quantity"] == "p_mw" else 0)
        assert not report["redispatch_performed"]
        assert report["held_quantity"] in ("p_mw", "q_mvar")


def test_generation_reproducible_and_archive_hashes_valid(run, tmp_path):
    dataset, output, config = run
    second = tmp_path / "same"
    generate_simulation(config, second)
    assert (second / "manifest.json").read_bytes() == (dataset / "manifest.json").read_bytes()
    for item in read(output / "completion.json")["files"]:
        assert hashlib.sha256((output / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_edited_unsealed_profiles_fail_without_solver(run, tmp_path):
    _, _, config = run
    changed = tmp_path / "changed"
    generate_simulation(config, changed)
    (changed / "plant.json").write_text("{}", encoding="utf-8")
    with patch(
        "oilfield_energy.bootstrap.adapters.simulation_runner.solve_case_ac_consistent",
        side_effect=AssertionError("solver called"),
    ):
        result = create_simulation().execute(changed / "manifest.json", tmp_path / "failed")
    assert result.exit_code == 2
    assert "HASH_MISMATCH" in read(result.output / "study_summary.json")["error"]


def test_same_manifest_feeds_all_study_phases(run):
    dataset, _, _ = run
    captured = CaptureDataset(LocalDatasetReader(), JsonManifestDecoder()).capture(
        dataset / "manifest.json"
    )
    spec, day, intra, plant = load_study(captured)
    assert spec.dataset_id == captured.manifest.dataset_id
    assert day.start == intra.start == plant.start


@pytest.mark.parametrize(
    "raw", [b'{"seed":1,"seed":2}', b'{"value":NaN}', b'{"outer":{"x":1,"x":2}}']
)
def test_duplicate_and_nonstandard_json_rejected(raw):
    with pytest.raises(ValueError):
        validate_json(raw)

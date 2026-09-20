"""Split datasets, actual AC evaluation, update and replay integration."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from oilfield_energy.bootstrap.adapters.legacy_snapshot import LegacySnapshotEngine
from oilfield_energy.bootstrap.field_dataset import create_field_dataset_workflow
from oilfield_energy.modules.measurements.adapters.local_files import LocalDatasetReader
from oilfield_energy.modules.measurements.adapters.manifest_json import JsonManifestDecoder
from oilfield_energy.modules.measurements.api import CaptureDataset
from oilfield_energy.modules.measurements.contracts import DatasetError
from oilfield_energy.workflows.field_dataset.adapters.result_directory import DirectoryResultStore
from oilfield_energy.workflows.field_dataset.contracts import Artifact

ROOT = Path(__file__).resolve().parents[2]
AT = datetime.fromisoformat("2026-09-13T12:00:00+08:00")


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "source"
    shutil.copytree(ROOT / "tests/fixtures/field_dataset", path)
    return path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh(dataset):
    manifest = read(dataset / "manifest.json")
    for ref in manifest["files"]:
        ref["sha256"] = hashlib.sha256((dataset / ref["path"]).read_bytes()).hexdigest()
    write(dataset / "manifest.json", manifest)


def execute(dataset, output, operation="run", demo=True, at=AT, mode=None):
    return create_field_dataset_workflow().execute(
        dataset / "manifest.json", output, operation=operation, demo=demo, at=at, mode=mode
    )


def test_single_manifest_reconstructs_fixed_state_without_optimization(dataset, tmp_path):
    with (
        patch("oilfield_energy.model.solve_case", side_effect=AssertionError("optimizer called")),
        patch(
            "oilfield_energy.bootstrap.adapters.project_dataset.build_synthetic_case",
            side_effect=AssertionError("fallback called"),
        ),
    ):
        result = execute(dataset, tmp_path / "run")
    assert result.status == "secure"
    report = read(result.output / "power_flow.json")
    point, flow = report["operating_point"], report["flow"]
    assert flow["pcc_import_mw"] == pytest.approx(
        sum(point["p_demand_mw_by_bus"].values()) + flow["loss_mw"], abs=1e-8
    )
    assert flow["pcc_import_mw"] == pytest.approx(3.716104325998246)
    assert len(report["snapshot"]["devices"]) == 5
    assert {d["resource_id"] for d in report["snapshot"]["devices"]} >= {"WT1", "WT2"}
    assert not report["redispatch_performed"]
    assert flow["branches"][0]["kind"] == "transformer"
    assert (result.output / "completion.json").is_file()


def test_replay_is_independent_of_original_files_and_paths(dataset, tmp_path):
    first = execute(dataset, tmp_path / "first")
    before = read(first.output / "power_flow.json")["flow"]
    (dataset / "network.json").write_text("invalid changed source", encoding="utf-8")
    second = execute(first.output / "dataset", tmp_path / "second")
    assert second.status == "secure"
    assert read(second.output / "power_flow.json")["flow"] == before
    for item in read(first.output / "completion.json")["files"]:
        assert (
            hashlib.sha256((first.output / item["path"]).read_bytes()).hexdigest() == item["sha256"]
        )


def test_network_change_only_needs_data_update_and_explicit_seal(dataset, tmp_path):
    first = execute(dataset, tmp_path / "first")
    document = read(dataset / "network.json")
    document["version"] = "2"
    document["payload"]["branches"][1]["r_pu"] *= 2
    write(dataset / "network.json", document)
    manifest = read(dataset / "manifest.json")
    manifest["revision"] = "demo-2"
    manifest["files"][0]["version"] = "2"
    write(dataset / "manifest.json", manifest)
    failed = execute(dataset, tmp_path / "failed")
    assert failed.exit_code == 2
    assert "HASH_MISMATCH" in str(read(failed.output / "dataset_run.json"))
    sealed = execute(dataset, tmp_path / "sealed", operation="seal")
    assert sealed.status == "validated"
    assert not (sealed.output / "power_flow.json").exists()
    second = execute(sealed.output / "dataset", tmp_path / "second")
    assert second.exit_code == 0
    assert (
        read(second.output / "power_flow.json")["flow"]["loss_mw"]
        != read(first.output / "power_flow.json")["flow"]["loss_mw"]
    )
    assert read(first.output / "dataset/manifest.json")["revision"] == "demo-1"


@pytest.mark.parametrize(
    "file,change,reason",
    [
        ("devices.json", lambda d: d.update(dataset_id="wrong-site"), "DOCUMENT_IDENTITY_MISMATCH"),
        ("devices.json", lambda d: d.update(version="wrong-version"), "DOCUMENT_IDENTITY_MISMATCH"),
        (
            "devices.json",
            lambda d: d["payload"]["devices"][0].update(bus_id="absent"),
            "DEVICE_BUS_UNKNOWN",
        ),
        ("point_mapping.json", lambda d: d["payload"]["points"].pop(), "POINT_COVERAGE_MISMATCH"),
        (
            "point_mapping.json",
            lambda d: d["payload"]["sources"][0].update(file_id="network"),
            "SOURCE_FILE_ROLE_INVALID",
        ),
        (
            "operating_modes.json",
            lambda d: d["payload"].update(network_id="other"),
            "OPERATING_MODE_NETWORK_MISMATCH",
        ),
        ("settings.json", lambda d: d["payload"].update(mapping_confirmed=False), "UNCONFIRMED"),
        ("network.json", lambda d: d["payload"]["branches"][0].update(unknown=1), "unknown fields"),
    ],
)
def test_cross_file_validation_blocks_bad_input(dataset, tmp_path, file, change, reason):
    doc = read(dataset / file)
    change(doc)
    write(dataset / file, doc)
    refresh(dataset)
    outcome = execute(dataset, tmp_path / "failed", operation="check")
    assert outcome.exit_code == 2
    assert reason in str(read(outcome.output / "dataset_run.json"))


def test_bad_measurement_and_nonconvergence_not_reported_as_safe(dataset, tmp_path):
    path = dataset / "measurements.csv"
    path.write_text(path.read_text().replace("GOOD", "BAD", 1), encoding="utf-8")
    refresh(dataset)
    result = execute(dataset, tmp_path / "bad")
    assert result.exit_code == 2
    assert read(result.output / "power_flow.json")["flow"] is None
    path.write_text(path.read_text().replace("BAD", "GOOD", 1), encoding="utf-8")
    settings = read(dataset / "settings.json")
    settings["payload"]["limits"]["max_iterations"] = 1
    write(dataset / "settings.json", settings)
    refresh(dataset)
    result = execute(dataset, tmp_path / "not-converged")
    assert result.status == "not_converged"
    assert read(result.output / "power_flow.json")["flow"]["pcc_import_mw"] is None


def test_overload_preserved_without_clipping(dataset, tmp_path):
    doc = read(dataset / "network.json")
    doc["payload"]["branches"][0]["s_max_mva"] = 0.1
    write(dataset / "network.json", doc)
    refresh(dataset)
    result = execute(dataset, tmp_path / "overload")
    assert result.status == "violation"
    assert read(result.output / "power_flow.json")["flow"]["violations"]


def test_demo_field_and_operating_time_gates(dataset, tmp_path):
    assert execute(dataset, tmp_path / "no-demo", demo=False).exit_code == 2
    assert execute(dataset, tmp_path / "bad-mode", mode="missing").exit_code == 2
    assert execute(dataset, tmp_path / "bad-time", at=AT.replace(day=14)).exit_code == 2
    result = execute(dataset, tmp_path / "check", operation="check")
    assert result.status == "validated"
    assert read(result.output / "validation.json")["network_safety"] == "unknown"


def test_engine_never_rereads_live_sources_after_capture(dataset):
    captured = CaptureDataset(LocalDatasetReader(), JsonManifestDecoder()).capture(
        dataset / "manifest.json"
    )
    (dataset / "measurements.csv").write_bytes(b"corrupted after capture")
    assert (
        LegacySnapshotEngine().evaluate(captured, at=AT, mode=None, demo=True, solve=True).status
        == "secure"
    )


def test_store_refuses_reusing_output_and_marks_partial_failure(tmp_path):
    store = DirectoryResultStore()
    output = tmp_path / "result"
    store.publish(output, (Artifact("value.txt", b"one"),))
    with pytest.raises(FileExistsError):
        store.publish(output, (Artifact("value.txt", b"two"),))
    assert (output / "value.txt").read_bytes() == b"one"
    with patch.object(Path, "replace", side_effect=OSError("disk failed")):
        with pytest.raises(OSError):
            store.publish(tmp_path / "partial", (Artifact("value.txt", b"one"),))
    assert not (tmp_path / "partial/completion.json").exists()


def test_cli_works_outside_repository_without_pythonpath(dataset, tmp_path):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    cmd = [
        sys.executable,
        "-m",
        "oilfield_energy.entrypoints.field_dataset",
        "run",
        "--manifest",
        str(dataset / "manifest.json"),
        "--output",
        str(tmp_path / "cli"),
        "--at",
        AT.isoformat(),
        "--demo",
    ]
    result = subprocess.run(cmd, env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_reader_rejects_symlink_outside_dataset(dataset, tmp_path):
    external = tmp_path / "external.json"
    external.write_text("external")
    link = dataset / "link.json"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("OS does not grant symlink creation")
    with pytest.raises(DatasetError, match="OUTSIDE_DATASET"):
        LocalDatasetReader().read_file(dataset / "manifest.json", "link.json")


@pytest.mark.parametrize("unknown", [False, True])
def test_mode_selection_overlap_or_unknown_rejected(dataset, tmp_path, unknown):
    document = read(dataset / "operating_modes.json")
    selection = dict(document["payload"]["selections"][0])
    if unknown:
        selection["mode_id"] = "not-in-network"
    document["payload"]["selections"].append(selection)
    write(dataset / "operating_modes.json", document)
    refresh(dataset)
    outcome = execute(dataset, tmp_path / "bad-mode", operation="check")
    assert outcome.status == "invalid_input"
    assert "OPERATING_MODE_SELECTION_" in str(read(outcome.output / "dataset_run.json"))


def test_archive_file_ids_cannot_collide_through_extensions(dataset, tmp_path):
    manifest = read(dataset / "manifest.json")
    for file_id, path in (("drawing", "drawing.pdf"), ("drawing.pdf", "drawing")):
        (dataset / path).write_bytes(file_id.encode())
        manifest["files"].append(
            dict(file_id=file_id, role="attachment", path=path, version="1", sha256=None)
        )
    write(dataset / "manifest.json", manifest)
    refresh(dataset)
    outcome = execute(dataset, tmp_path / "attachments")
    assert outcome.status == "secure"
    assert (outcome.output / "dataset/files/drawing/content.pdf").read_bytes() == b"drawing"
    assert (outcome.output / "dataset/files/drawing.pdf/content").read_bytes() == b"drawing.pdf"
    runtime = read(outcome.output / "runtime.json")
    assert len(runtime["implementation_sha256"]) == 64
    assert runtime["dependencies"]["oilfield-energy-optimization"] == "0.1.0"

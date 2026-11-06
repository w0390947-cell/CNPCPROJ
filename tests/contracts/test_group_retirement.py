"""The retired feature cannot be requested; remaining results retain their evidence."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from oilfield_energy.service import ScenarioType, SimulationRequest, SimulationResult
from oilfield_energy.web_api import app

ROOT = Path(__file__).resolve().parents[2]


def test_retired_scenario_is_rejected_before_job_creation():
    assert {s.value for s in ScenarioType} == {
        "single_microgrid",
        "cluster_coordination",
        "communication_fault",
    }
    with pytest.raises(ValidationError):
        SimulationRequest(scenario_type="group_control")
    schemas = app.openapi()["components"]["schemas"]
    assert "GroupControlSummary" not in schemas
    assert "group_control" not in schemas["SimulationResult"]["properties"]


def test_retired_command_is_not_advertised_or_accepted(tmp_path):
    help_result = subprocess.run(
        [sys.executable, "-m", "oilfield_energy.cli", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "group-scenarios" not in help_result.stdout
    rejected = subprocess.run(
        [sys.executable, "-m", "oilfield_energy.cli", "group-scenarios"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0


def test_result_migration_preserves_evidence_and_original(tmp_path):
    payload = json.loads(
        (ROOT / "web/frontend/tests/fixtures/synthetic-single-result.json").read_text(
            encoding="utf-8"
        )
    )
    payload["metadata"]["schema_version"] = "1.26.0"
    payload["group_control"] = None
    source, output = tmp_path / "old.json", tmp_path / "new.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    original = source.read_bytes()
    command = [
        sys.executable,
        str(ROOT / "tools/migrate_simulation_result_v2.py"),
        str(source),
        str(output),
    ]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    migrated = json.loads(output.read_text(encoding="utf-8"))
    assert source.read_bytes() == original
    assert migrated["single_execution"] == payload["single_execution"]
    assert migrated["timeseries"] == payload["timeseries"]
    assert migrated["reference_economics"] == payload["reference_economics"]
    assert migrated["metadata"]["schema_version"] == "2.0.0"
    SimulationResult.model_validate(migrated)
    assert subprocess.run(command, capture_output=True).returncode != 0


def test_migration_rejects_retired_results(tmp_path):
    source, output = tmp_path / "group.json", tmp_path / "new.json"
    source.write_text(
        json.dumps(
            {"metadata": {"schema_version": "1.26.0", "scenario_type": "group_control"}}
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/migrate_simulation_result_v2.py"),
            str(source),
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "historical artifacts" in result.stderr
    assert not output.exists()

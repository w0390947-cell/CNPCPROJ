"""Production rejects historical single records at its public read boundaries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from oilfield_energy.bootstrap.adapters.demo_jobs import DemoJobs
from oilfield_energy.service import SimulationResult
from oilfield_energy.web_api import get_simulation_result

FIXTURE = (
    Path(__file__).resolve().parents[3] / "web/frontend/tests/fixtures/synthetic-single-result.json"
)


@pytest.mark.parametrize("version", [None, "1.0.0", "1.17.0", "1.18.0"])
def test_old_or_absent_single_schema_version_is_rejected(version):
    raw = json.loads(FIXTURE.read_bytes())
    raw["metadata"]["schema_version"] = version
    if version is None:
        del raw["metadata"]["schema_version"]
    with pytest.raises(ValueError, match="archived environment"):
        SimulationResult.model_validate(raw)


def test_http_old_result_read_returns_explicit_conflict():
    raw = json.loads(FIXTURE.read_bytes())
    raw["metadata"]["schema_version"] = "1.17.0"

    class Jobs:
        def read_result(self, _identity):
            return json.dumps(raw)

    with pytest.raises(HTTPException) as caught:
        get_simulation_result("historical", Jobs())
    assert caught.value.status_code == 409
    assert "归档" in caught.value.detail


def test_demo_result_reader_rejects_old_results_as_expected_failure():
    raw = json.loads(FIXTURE.read_bytes())
    raw["metadata"]["schema_version"] = "1.17.0"
    jobs = SimpleNamespace(manager=SimpleNamespace(read_result=lambda _: json.dumps(raw)))
    with pytest.raises(RuntimeError, match="archive"):
        DemoJobs.result(jobs, "historical")

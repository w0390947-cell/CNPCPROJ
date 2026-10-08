"""Generated schema and safety provenance of the new single result."""

import json
from pathlib import Path

import pytest

from oilfield_energy.bootstrap.adapters.cluster_rolling import window_case
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation
from oilfield_energy.workflows.single_execution.contracts import SingleExecution


@pytest.fixture(scope="module")
def evidence():
    case = build_synthetic_case(96)
    energy = {m.name: m.storage.e_initial_mwh for m in case.microgrids}
    result = run_simulation(
        SimulationRequest(steps=2), input_case=window_case(case, 0, 2, energy, energy)
    )
    assert result.single_execution.status == "passed"
    return result.single_execution.model_dump(mode="json")


def test_result_schema_matches_authority():
    root = Path(__file__).resolve().parents[3]
    expected = json.loads(
        (root / "contracts/simulation/result.schema.json").read_text(encoding="utf-8")
    )
    assert expected == SimulationResult.model_json_schema()


@pytest.mark.parametrize(
    "change",
    [
        "reset",
        "missing_minute",
        "missing_certificate",
        "wrong_command",
        "old_policy",
        "startup_claim",
    ],
)
def test_false_complete_or_dynamic_certificate_is_rejected(evidence, change):
    data = json.loads(json.dumps(evidence))
    if change == "reset":
        data["unified"]["economic_proposals"][1]["initial"]["energy_mwh"] += 0.1
    elif change == "missing_minute":
        data["completed_minutes"] -= 1
    elif change == "missing_certificate":
        data["unified"]["plans"].clear()
    elif change == "wrong_command":
        data["unified"]["steps"][0]["command"]["p_mw"][0] += 0.1
    elif change == "old_policy":
        data["policy"]["single_execution_version"] = None
    else:
        data["startup_transition_assessed"] = True
    with pytest.raises(ValueError):
        SingleExecution.model_validate(data)


@pytest.mark.parametrize("version", [None, "single-execution-v1", "single-execution-v2", "single-execution-v3"])
def test_old_or_unversioned_execution_is_rejected(evidence, version):
    data = {**evidence, "version": version}
    if version is None:
        del data["version"]
    with pytest.raises(ValueError):
        SingleExecution.model_validate(data)


@pytest.mark.parametrize(
    "field", ["windows", "minute", "reactive_dynamics", "conditional_guarantee"]
)
def test_removed_evidence_fields_are_not_ignored(evidence, field):
    with pytest.raises(ValueError):
        SingleExecution.model_validate({**evidence, field: None})


def test_removed_request_field_is_rejected_even_when_null():
    with pytest.raises(ValueError, match="Extra inputs"):
        SimulationRequest.model_validate({"conditional_guarantee": None})

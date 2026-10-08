"""Versioned ordinary-Q evidence and historical policy compatibility."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from oilfield_energy.modules.control.api import assess_reactive_cycle
from oilfield_energy.modules.control.contracts import ReactiveDynamicsRecord
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy

ROOT = Path(__file__).resolve().parents[3]


def test_reactive_dynamics_record_roundtrips_and_schema_has_no_drift():
    record = assess_reactive_cycle(
        resource_ids=("wind",),
        initial_mvar=(1.5,),
        targets_mvar=(-1.305,),
        actual_mvar=(0.6,),
        minimum_mvar=(-1.305,),
        maximum_mvar=(1.305,),
        alpha=0.4,
        ramp_mvar=0.9,
        response_applied=True,
    )
    adapter = TypeAdapter(ReactiveDynamicsRecord)
    assert adapter.validate_json(adapter.dump_json(record)) == record
    schema = json.loads(
        (ROOT / "contracts/control/reactive_dynamics.schema.json").read_text(encoding="utf-8")
    )
    assert schema == adapter.json_schema()
    assert record.ramp_compliant and record.response_matches_prediction


def test_historical_policy_does_not_acquire_new_dynamics_evidence():
    assert ExecutionPolicy.model_validate({}).reactive_dynamics_version is None
    policy = ExecutionPolicy(reactive_dynamics_version="reactive-dynamics-v1")
    assert ExecutionPolicy.model_validate_json(policy.model_dump_json()) == policy

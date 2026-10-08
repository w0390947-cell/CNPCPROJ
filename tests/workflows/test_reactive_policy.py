"""Historical policy evidence must not inherit the corrected model's proof."""

from oilfield_energy.modules.dispatch.contracts import ReactivePlanningPolicy
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


def test_new_policy_is_versioned_and_roundtrips_without_loss():
    current = ExecutionPolicy(reactive_planning=ReactivePlanningPolicy())
    assert current.reactive_planning is not None
    assert current.reactive_planning.version == "reactive-planning-v2"
    assert ExecutionPolicy.model_validate_json(current.model_dump_json()) == current


def test_legacy_and_missing_policy_records_do_not_acquire_new_evidence():
    assert ExecutionPolicy.model_validate({}).reactive_planning is None
    for record in ({}, {"version": "reactive-planning-v1"}):
        original = dict(record)
        restored = ExecutionPolicy.model_validate({"reactive_planning": record})
        assert restored.reactive_planning is not None
        assert restored.reactive_planning.version == "reactive-planning-v1"
        assert record == original

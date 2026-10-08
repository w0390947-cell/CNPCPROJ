"""Capability checks have their own version; historical records remain unknown."""

from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


def test_device_capability_version_is_explicit_and_roundtrips():
    assert ExecutionPolicy.model_validate({}).device_capability_version is None
    policy = ExecutionPolicy(device_capability_version="device-capability-v2")
    assert ExecutionPolicy.model_validate_json(policy.model_dump_json()) == policy

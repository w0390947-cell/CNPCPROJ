"""The policy drives execution and new evidence never appears on old records."""

from oilfield_energy.bootstrap.adapters.cluster_rolling import execution_time_scale
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy, RegionalExecution


def test_recorded_policy_drives_the_same_device_response_and_ramp_settings():
    dynamic = DynamicTrackingPolicy(
        time_constant_minutes=3,
        pv_time_constant_minutes=0.5,
        p_ramp_mw_per_minute=0.4,
        q_ramp_mvar_per_minute=0.3,
    )
    policy = ExecutionPolicy(dynamic_tracking=dynamic)
    config = execution_time_scale(policy)
    assert config.device_time_constant_minutes == 3
    assert config.pv_device_time_constant_minutes == 0.5
    assert config.active_power_ramp_mw_per_minute == 0.4
    assert config.reactive_power_ramp_mvar_per_minute == 0.3
    assert ExecutionPolicy.model_validate_json(policy.model_dump_json()) == policy


def test_legacy_records_remain_without_dynamic_policy_or_assessment():
    assert ExecutionPolicy.model_validate({}).dynamic_tracking is None
    region = RegionalExecution.model_validate({"region": "SC", "status": "unknown"})
    assert region.dynamic_tracking == {}

"""The policy drives execution and new evidence never appears on old records."""

from math import exp
from oilfield_energy.bootstrap.adapters.cluster_safety import (
    build_cluster_safety_domain,
)
from oilfield_energy.workflows.cluster_execution.contracts import ClusterSafetyPolicy
from tests.support.reserve_case import stationary_reserve_case
import pytest
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    RegionalExecution,
)


def test_recorded_policy_drives_the_same_device_response_and_ramp_settings():
    dynamic = DynamicTrackingPolicy(
        time_constant_minutes=3,
        pv_time_constant_minutes=0.5,
        p_ramp_mw_per_minute=0.4,
        q_ramp_mvar_per_minute=0.3,
    )
    policy = ExecutionPolicy(
        dynamic_tracking=dynamic, cluster_safety=ClusterSafetyPolicy()
    )
    domain = build_cluster_safety_domain(stationary_reserve_case(), policy, True)
    for region in domain.regions:
        model = region.devices
        assert model.alpha_q == pytest.approx(1 - exp(-1 / 3))
        assert model.aggregate_q_ramp_mvar == 0.3
        assert next(
            r for r in model.resources if r.kind == "storage"
        ).alpha_p == pytest.approx(1 - exp(-1 / 3))
        assert all(
            r.alpha_p == pytest.approx(1 - exp(-1 / 0.5))
            for r in model.resources
            if r.kind == "pv"
        )
        assert sum(
            r.ramp_p_mw for r in model.resources if r.kind == "pv"
        ) == pytest.approx(0.4)
    assert ExecutionPolicy.model_validate_json(policy.model_dump_json()) == policy


def test_legacy_records_remain_without_dynamic_policy_or_assessment():
    assert ExecutionPolicy.model_validate({}).dynamic_tracking is None
    region = RegionalExecution.model_validate({"region": "SC", "status": "unknown"})
    assert region.dynamic_tracking == {}

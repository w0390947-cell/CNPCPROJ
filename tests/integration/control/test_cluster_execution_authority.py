"""Current device capability and fail-closed execution after legacy retirement."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from oilfield_energy.bootstrap.adapters.cluster_safety import (
    build_cluster_safety_domain,
)
from oilfield_energy.bootstrap.adapters.cluster_rolling import RollingClusterEngine
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig
from oilfield_energy.modules.control.api import (
    certify_cluster_device_transition,
    point_box,
)
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy, TubeCommand
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    ClusterSafetyPolicy,
)
from tests.support.reserve_case import stationary_reserve_case


@pytest.mark.parametrize(
    "low,high,valid,invalid", [(-0.1, 2.0, 1.0, -0.2), (-2.0, 0.1, -1.0, 0.2)]
)
def test_cluster_svg_command_and_entire_device_path_preserve_signed_bounds(
    low, high, valid, invalid
):
    case = stationary_reserve_case()
    case = replace(
        case,
        microgrids=[
            replace(m, svg_q_min_mvar=low, svg_q_max_mvar=high, svg_s_max_mva=2.0)
            for m in case.microgrids
        ],
    )
    policy = ExecutionPolicy(
        cluster_safety=ClusterSafetyPolicy(), dynamic_tracking=DynamicTrackingPolicy()
    )
    domain = build_cluster_safety_domain(case, policy, True)
    for region in domain.regions:
        index = next(
            i for i, r in enumerate(region.devices.resources) if r.kind == "svg"
        )
        svg = region.devices.resources[index]
        assert (svg.q.low, svg.q.high) == (low, high)
        q = list(region.initial.q_mvar)
        q[index] = valid
        proof = certify_cluster_device_transition(
            region.devices,
            point_box(region.initial),
            TubeCommand(p_mw=region.initial.p_mw, q_mvar=tuple(q)),
        )
        assert (
            low <= proof.path.q_mvar[index].low <= proof.path.q_mvar[index].high <= high
        )
        assert (
            max(
                abs(proof.endpoint.q_mvar[index].low),
                abs(proof.endpoint.q_mvar[index].high),
            )
            > 0.1
        )
        q[index] = invalid
        with pytest.raises(ValueError, match="device path capability"):
            certify_cluster_device_transition(
                region.devices,
                point_box(region.initial),
                TubeCommand(p_mw=region.initial.p_mw, q_mvar=tuple(q)),
            )


def engine_arguments(policy):
    case = stationary_reserve_case()
    return dict(
        case=case,
        original_case=case,
        plants={},
        original_admm=SimpleNamespace(),
        day_ahead=(),
        storage_enabled=False,
        policy=policy,
        admm_config=ADMMConfig(),
        communication_config=CommunicationConfig(),
        loss_calibration=None,
        solve_regions=lambda *_: pytest.fail(
            "unavailable safety must prevent planning"
        ),
        progress=lambda _: None,
    )


def test_direct_rolling_construction_cannot_disable_safety_to_run_a_legacy_executor():
    with pytest.raises(ValueError, match="requires the certified safety policy"):
        RollingClusterEngine(**engine_arguments(ExecutionPolicy()))


@pytest.mark.parametrize("failure_stage", ["domain", "checkpoint"])
def test_failed_safety_initialization_cannot_plan_advance_or_report_success(
    monkeypatch,
    failure_stage,
):
    from oilfield_energy.bootstrap.adapters import cluster_rolling

    def reject(*args, **kwargs):
        raise ValueError("injected uncertified initial domain")

    if failure_stage == "domain":
        monkeypatch.setattr(cluster_rolling, "build_cluster_safety_domain", reject)
    else:
        # Allocation alone must not authorize a port with an invalid checkpoint.
        monkeypatch.setattr(
            cluster_rolling,
            "ClusterSafetyPort",
            lambda *a, **k: SimpleNamespace(checkpoints=reject),
        )
    engine = RollingClusterEngine(
        **engine_arguments(
            ExecutionPolicy(
                cluster_safety=ClusterSafetyPolicy(),
                dynamic_tracking=DynamicTrackingPolicy(),
            )
        )
    )
    update = engine.prepare(0, 15, 30)
    assert not update.adopted and not update.continuation_available
    assert "uncertified initial domain" in update.reason
    with pytest.raises(RuntimeError, match="safety initialization failed"):
        engine.advance(0, 1)
    assert all(r.status == "unknown" and not r.p_mw for r in engine.intraday_evidence())

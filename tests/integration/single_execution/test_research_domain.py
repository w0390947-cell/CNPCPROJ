"""Bounds are constructed before observing the independent minute trajectories."""

from dataclasses import replace

import pytest

from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.workflows.single_execution.api import validate_unified_observation


def test_single_projection_cannot_inherit_cluster_storage_response_certificates():
    from oilfield_energy.bootstrap.adapters.cluster_rolling import (
        with_storage_reachability,
    )
    from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
    from oilfield_energy.bootstrap.adapters.single_planning import (
        prepare_single_case,
        single_execution_policy,
    )
    from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy
    from oilfield_energy.modules.dispatch.contracts import StorageReachabilityPolicy

    original = with_storage_reachability(
        build_synthetic_case(96),
        policy=StorageReachabilityPolicy(),
        dynamics=DynamicTrackingPolicy(),
        checkpoints={},
        storage_enabled=True,
        execution_step_minutes=1,
    )
    assert len(original.storage_reachability) == 3
    single = prepare_single_case(
        original, "SC", storage_enabled=True, policy=single_execution_policy()
    )
    assert not single.storage_reachability
    assert [m.name for m in single.microgrids] == ["SC"]
    assert len(original.storage_reachability) == 3


@pytest.mark.parametrize("region", ["SC", "YA_B", "YA_C"])
def test_declared_forecast_bounds_cover_seeded_inputs_without_reading_future_observations(
    monkeypatch, region
):
    import oilfield_energy.bootstrap.adapters.single_unified as adapter

    class Captured(Exception):
        pass

    def inspect(domain, initial, **ports):
        assert len(domain.forecasts) == 1440
        assert domain.research.load_relative_error == 0.025
        for step in range(1440):
            validate_unified_observation(domain, step, ports["observe"](step))
        # PV retains its daytime contribution; a whole-day minimum is not used.
        pv = {r.resource_id for r in domain.devices.resources if r.kind == "pv"}
        assert any(
            g.minimum_available_mw > 0
            for f in domain.forecasts
            for g in f.generation
            if g.resource_id in pv
        )
        assert all(
            g.minimum_available_mw == 0
            for g in domain.forecasts[0].generation
            if g.resource_id in pv
        )
        raise Captured()

    monkeypatch.setattr(adapter, "run_unified_feedback", inspect)
    with pytest.raises(Captured):
        run_simulation(SimulationRequest(region=region, steps=8))


def test_resource_projection_preserves_distinct_wind_ratios_asymmetric_svg_and_total_pv_ramp():
    from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
    from oilfield_energy.bootstrap.adapters.single_model import (
        assemble_single_physical_model,
    )
    from oilfield_energy.bootstrap.adapters.single_planning import (
        single_execution_policy,
    )

    case = build_synthetic_case(96)
    mg = case.microgrids[0]
    buses = tuple(mg.wind_capacity_mw)
    mg = replace(
        mg,
        svg_q_min_mvar=-0.1,
        svg_q_max_mvar=2,
        wind_q_abs_over_p_max={buses[0]: 0.2, buses[1]: 0.3},
    )
    policy = single_execution_policy()
    _, devices = assemble_single_physical_model(mg, case, policy, storage_enabled=True)
    resources = devices.resources
    assert [r.q_over_p for r in resources if r.kind == "wind"] == [0.2, 0.3]
    svg = next(r for r in resources if r.kind == "svg")
    assert (svg.q.low, svg.q.high) == (-0.1, 2)
    assert sum(r.ramp_p_mw for r in resources if r.kind == "pv") == pytest.approx(
        policy.dynamic_tracking.p_ramp_mw_per_minute
    )

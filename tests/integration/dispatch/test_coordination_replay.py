"""Real synthetic ADMM evidence: no UI fixture is used as numerical proof."""

import json

import numpy as np
import pytest
from pydantic import TypeAdapter

from oilfield_energy.admm import run_admm_coordination
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.hierarchy_types import (
    ADMMConfig,
    ADMMIteration,
    CommunicationConfig,
)
from oilfield_energy.service import ADMMHistoryPoint, _admm_history


def test_trace_matches_final_reference_residual_and_survives_mutation():
    case = build_synthetic_case(4)
    result = run_admm_coordination(case, admm_config=ADMMConfig(max_iterations=220))
    assert result.converged
    for item in result.history:
        trace = item.coordination
        assert trace.communication_tick == item.iteration
        assert trace.time_hours == tuple(case.time_hours)
        assert trace.global_updated
        assert sum(region.fresh for region in trace.regions) == item.fresh_region_count
        residual_squared = sum(
            sum(
                (p - z) ** 2
                for p, z in zip(region.proposal_p_mw, region.reference_p_mw)
            )
            + sum(
                (q - z) ** 2
                for q, z in zip(region.proposal_q_mvar, region.reference_q_mvar)
            )
            for region in trace.regions
        )
        assert np.sqrt(residual_squared) == pytest.approx(
            item.primal_residual, abs=1e-10
        )
    last = result.history[-1].coordination
    for region in last.regions:
        np.testing.assert_allclose(
            region.reference_p_mw, result.p_references_mw[region.region]
        )
        np.testing.assert_allclose(
            region.reference_q_mvar, result.q_references_mvar[region.region]
        )
    first_values = result.history[0].coordination.regions[0].reference_p_mw
    result.p_references_mw[last.regions[0].region][0] = 999
    assert result.history[0].coordination.regions[0].reference_p_mw == first_values
    assert last.regions[0].reference_p_mw[0] != 999
    points = _admm_history(result)
    assert points[-1].coordination == last
    restored = ADMMHistoryPoint.model_validate_json(points[-1].model_dump_json())
    assert restored == points[-1]
    assert (
        ADMMHistoryPoint.model_validate(points[-1].model_dump(mode="json")) == restored
    )
    # The existing hierarchical JSON exporter also serializes the new evidence.
    json.dumps(
        TypeAdapter(ADMMIteration).dump_python(result.history[-1], mode="json"),
        allow_nan=False,
    )


def test_outage_records_held_reference_stale_proposal_and_recovery():
    result = run_admm_coordination(
        build_synthetic_case(4),
        admm_config=ADMMConfig(max_iterations=20, min_iterations=20),
        communication_config=CommunicationConfig(
            outage_region="YA_B",
            outage_start_iteration=3,
            outage_end_iteration=8,
            stale_limit_iterations=2,
        ),
    )
    for index, item in enumerate(result.history):
        trace = item.coordination
        region = next(region for region in trace.regions if region.region == "YA_B")
        assert region.outage == (3 <= item.iteration <= 8)
        assert sum(region.fresh for region in trace.regions) == item.fresh_region_count
        if region.outage:
            assert not trace.global_updated and not region.fresh
            assert region.response_sent_tick == 2
            previous = result.history[index - 1].coordination
            assert [r.reference_p_mw for r in trace.regions] == [
                r.reference_p_mw for r in previous.regions
            ]
        if 5 <= item.iteration <= 8:
            assert region.fallback
        if item.iteration == 9:
            assert region.fresh and not region.outage and not region.fallback


def test_no_received_proposal_is_unknown_not_autonomous_initial_power():
    result = run_admm_coordination(
        build_synthetic_case(4),
        admm_config=ADMMConfig(max_iterations=5),
        communication_config=CommunicationConfig(
            loss_probability=1, stale_limit_iterations=2
        ),
    )
    assert not result.converged
    for item in result.history:
        assert not item.coordination.global_updated
        for region in item.coordination.regions:
            assert region.proposal_p_mw is None and region.proposal_q_mvar is None
            assert region.response_epoch is None and region.response_sent_tick is None
            assert not region.fresh
    assert all(region.fallback for region in result.history[-1].coordination.regions)

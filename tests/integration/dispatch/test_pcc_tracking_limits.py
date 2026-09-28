"""Independent per-interval bounds, including a deliberately unhelpful objective."""

from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pytest

from oilfield_energy.ac_consistency import ACConsistencyConfig, solve_case_ac_consistent
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.model import solve_case
from oilfield_energy.modules.dispatch.contracts import PCCTrackingLimits
from tests.legacy_case_fixture import build_synthetic_case


@pytest.mark.parametrize("solver", [solve_case, solve_case_misocp])
def test_each_interval_obeys_custom_p_and_q_limits_even_without_penalties(solver):
    case = build_synthetic_case(4)
    options = dict(storage_enabled=False, cluster_coordination=False, time_limit_seconds=20)
    baseline = solver(case, ["SC"], **options)
    assert baseline.success
    data = baseline.microgrids["SC"]
    targets = {
        "SC": {
            "p_mw": data["p_grid_mw"] + 0.3,
            "q_mvar": data["q_grid_mvar"] - 0.1,
        }
    }
    saved = {key: value.copy() for key, value in targets["SC"].items()}
    limits = PCCTrackingLimits(0.02, 0.03, 1e-6)
    result = solver(
        case,
        ["SC"],
        pcc_targets=targets,
        tracking_limits=limits,
        p_tracking_penalty_cny_per_mw=0,
        q_tracking_penalty_cny_per_mvar=0,
        **options,
    )
    assert result.success
    # Compare actual grid powers to input references, not optimizer slack variables.
    np.testing.assert_array_less(
        abs(result.microgrids["SC"]["p_grid_mw"] - targets["SC"]["p_mw"]), 0.020001
    )
    np.testing.assert_array_less(
        abs(result.microgrids["SC"]["q_grid_mvar"] - targets["SC"]["q_mvar"]), 0.030001
    )
    for key, values in saved.items():
        np.testing.assert_array_equal(targets["SC"][key], values)


@pytest.mark.parametrize("solver", [solve_case, solve_case_misocp])
@pytest.mark.parametrize("axis", ["p_mw", "q_mvar"])
def test_one_impossible_interval_is_infeasible_instead_of_returning_a_soft_plan(solver, axis):
    case = build_synthetic_case(4)
    targets = {"SC": {"p_mw": np.full(4, 4.0), "q_mvar": np.zeros(4)}}
    targets["SC"][axis][2] = -100
    result = solver(
        case,
        ["SC"],
        pcc_targets=targets,
        tracking_limits=PCCTrackingLimits(0.05, 0.05, 1e-6),
        time_limit_seconds=20,
    )
    assert not result.success and not result.microgrids
    assert "infeasible" in result.message.lower()


@pytest.mark.parametrize("solver", [solve_case, solve_case_misocp])
def test_hard_limits_require_targets_for_every_selected_region(solver):
    with pytest.raises(ValueError, match="requires PCC targets"):
        solver(build_synthetic_case(4), tracking_limits=PCCTrackingLimits(0.05, 0.05, 1e-6))


@pytest.mark.parametrize("solver", [solve_case, solve_case_misocp])
@pytest.mark.parametrize("bad", [np.full(4, np.nan), np.full(4, np.inf), np.zeros((4, 1))])
def test_invalid_target_vectors_are_rejected_before_solving(solver, bad):
    with pytest.raises(ValueError, match="finite vectors"):
        solver(
            build_synthetic_case(4),
            ["SC"],
            pcc_targets={"SC": {"p_mw": np.ones(4), "q_mvar": bad}},
            tracking_limits=PCCTrackingLimits(0.05, 0.05, 1e-6),
        )


@pytest.mark.parametrize("solver", [solve_case, solve_case_misocp])
def test_other_callers_can_explicitly_retain_soft_tracking(solver):
    result = solver(
        build_synthetic_case(4),
        ["SC"],
        pcc_targets={"SC": {"p_mw": np.full(4, -100.0), "q_mvar": np.zeros(4)}},
        tracking_limits=None,
        time_limit_seconds=20,
    )
    assert result.success
    assert np.min(result.microgrids["SC"]["p_grid_mw"]) > 0


def test_ac_feedback_and_integer_retry_both_keep_the_limits():
    from oilfield_energy import ac_consistency

    case = build_synthetic_case(4)
    baseline = solve_case_ac_consistent(case, ["SC"], time_limit_seconds=20)
    assert baseline.passed
    certified = baseline.optimization
    relaxed = replace(
        certified, cluster={**certified.cluster, "storage_integrality_certified": False}
    )
    limits = PCCTrackingLimits(0.02, 0.03, 1e-6)
    targets = {
        "SC": {
            "p_mw": certified.microgrids["SC"]["p_grid_mw"],
            "q_mvar": certified.microgrids["SC"]["q_grid_mvar"],
        }
    }
    with (
        patch.object(
            ac_consistency, "solve_case_misocp", side_effect=[relaxed, certified] * 2
        ) as solve,
        patch.object(
            ac_consistency,
            "validate_ac_dispatch",
            side_effect=[
                {**baseline.ac_validation, "passed": False},
                baseline.ac_validation,
            ],
        ),
    ):
        result = solve_case_ac_consistent(
            case,
            ["SC"],
            pcc_targets=targets,
            tracking_limits=limits,
            consistency_config=ACConsistencyConfig(max_feedback_iterations=2),
        )
    assert result.passed and result.iterations == 2
    assert [c.kwargs["relax_storage_binaries"] for c in solve.call_args_list] == [True, False] * 2
    assert all(c.kwargs["tracking_limits"] is limits for c in solve.call_args_list)


@pytest.mark.parametrize(
    "termination,reason",
    [
        ("infeasible", "solver_infeasible"),
        ("timelimit", "solver_time_limit_without_plan"),
        ("inforunbd", "solver_failed"),
    ],
)
def test_only_proven_infeasibility_is_reported_as_infeasible(termination, reason):
    from oilfield_energy import ac_consistency
    from oilfield_energy.model import OptimizationResult

    failed = OptimizationResult(False, 2, f"SCIP status: {termination}", 0, 0, None, {}, {}, {})
    with patch.object(ac_consistency, "solve_case_misocp", return_value=failed):
        result = solve_case_ac_consistent(build_synthetic_case(4), ["SC"])
    assert not result.passed and result.stop_reason == reason

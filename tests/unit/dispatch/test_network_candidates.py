"""Independent small enumerations and P/Q geometry; no solver backend."""

from itertools import permutations

import pytest

from oilfield_energy.modules.dispatch.api import select_network_candidates
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    NetworkCandidatePolicy,
    PCCTrackingLimits,
    RegionalNetworkCandidate,
)


def candidate(name, p, q=None, number=1):
    p = tuple(p)
    return RegionalNetworkCandidate(
        region=name,
        source_round=number,
        time_minutes=tuple(range(len(p))),
        p_mw=p,
        q_mvar=tuple(q) if q is not None else (0.0,) * len(p),
        calibration=CoordinationNetworkCalibration((0.0,) * len(p), (0.0,) * len(p)),
    )


def choose(
    points, *, names=("A", "B", "C"), limit=15.0, target=0.95, budget=4096, excluded=frozenset()
):
    axis = points[0].time_minutes
    return select_network_candidates(
        candidates=tuple(points),
        names=names,
        time_minutes=axis,
        p_reference_mw={n: (1.0,) * len(axis) for n in names},
        q_reference_mvar={n: (0.0,) * len(axis) for n in names},
        import_limit_mw=limit,
        import_tolerance_mw=1e-6,
        power_factor_target=target,
        power_factor_tolerance=1e-9,
        power_factor_margin_mvar=(0.0,) * len(axis),
        tracking=PCCTrackingLimits(0.05, 0.05, 1e-6),
        policy=NetworkCandidatePolicy(max_combination_checks=budget),
        excluded=excluded,
    )


def test_joint_capacity_selects_a_combination_of_complete_witnesses():
    points = [
        candidate("A", [8]),
        candidate("A", [4], number=2),
        candidate("B", [4]),
        candidate("C", [4]),
    ]
    for ordered in permutations(points):
        result = choose(ordered)
        assert result.status == "selected"
        assert [(c.region, c.source_round) for c in result.candidates] == [
            ("A", 2),
            ("B", 1),
            ("C", 1),
        ]
        assert result.normalized_reference_distance == pytest.approx(180.0)


def test_signed_reactive_power_is_aggregated_before_power_factor_check():
    assert (
        choose([candidate("A", [5], [4]), candidate("B", [5], [-4]), candidate("C", [5])]).status
        == "selected"
    )
    assert (
        choose([candidate("A", [5], [4]), candidate("B", [5], [4]), candidate("C", [5])]).status
        == "no_joint_candidate"
    )


def test_whole_horizon_selection_does_not_splice_individually_good_slots():
    points = [
        candidate("A", [8, 2]),
        candidate("A", [2, 8], number=2),
        candidate("B", [4, 4]),
        candidate("C", [4, 4]),
    ]
    # Per-slot mixing could invent [2,2], but neither A trajectory meets 15 MW.
    assert choose(points).status == "no_joint_candidate"


def test_search_budget_and_exclusions_never_claim_physical_infeasibility():
    points = [
        candidate("A", [8]),
        candidate("A", [4], number=2),
        candidate("B", [4]),
        candidate("C", [4]),
    ]
    result = choose(points, budget=1)
    assert result.status == "budget_exhausted" and not result.search_complete
    assert not result.candidates
    result = choose(points, excluded=frozenset({(("A", 2), ("B", 1), ("C", 1))}))
    assert result.status == "no_joint_candidate" and result.search_complete


def test_missing_region_does_not_produce_a_partial_cluster():
    result = choose([candidate("A", [1])])
    assert result.status == "no_joint_candidate" and result.total_combinations == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_candidate_rejects_nonfinite_physical_values(bad):
    with pytest.raises(ValueError):
        candidate("A", [bad])


def test_candidate_owns_immutable_series_and_rejects_wrong_axis():
    values = [4.0]
    point = candidate("A", values)
    values[0] = 99.0
    assert point.p_mw == (4.0,)
    with pytest.raises(ValueError):
        point.p_mw = (99.0,)
    with pytest.raises(ValueError, match="aligned"):
        choose([point, candidate("B", [4.0, 4.0]), candidate("C", [4.0])])

from dataclasses import FrozenInstanceError, replace

import pytest

from oilfield_energy.modules.power_flow.api import (
    assess_cluster_import,
    summarize_cluster_validation,
)
from oilfield_energy.modules.power_flow.contracts import RegionalImport


def regions():
    return tuple(RegionalImport(n, (0.0, 1.0), (4.0, 5.0), (True, True)) for n in ("a", "b", "c"))


def assess(rows, **kwargs):
    return assess_cluster_import(
        stage="minute",
        expected_regions=("a", "b", "c"),
        trajectories=rows,
        limit_mw=14.0,
        **kwargs,
    )


def test_synchronous_sum_detects_violation_and_keeps_actual_peak():
    result = assess(regions())
    assert result.status == "violated"
    assert result.peak_import_mw == 15
    assert result.violation_steps == 1 and result.sample_count == 2
    with pytest.raises(FrozenInstanceError):
        result.status = "passed"


@pytest.mark.parametrize(
    "bad",
    [
        {"time_minutes": (1.0, 2.0)},
        {"time_minutes": (1.0, 1.0)},
        {"time_minutes": (0.0,)},
        {"import_mw": (4.0,)},
        {"import_mw": (float("nan"), 1.0)},
        {"import_mw": (float("inf"), 1.0)},
        {"valid": (True, False)},
        {"valid": ()},
        {"name": "a"},
    ],
)
def test_invalid_or_asynchronous_evidence_cannot_pass(bad):
    a, b, c = regions()
    result = assess((a, replace(b, **bad), c))
    assert result.status == "unknown"
    assert result.peak_import_mw is None and result.violation_steps is None


def test_missing_region_is_unknown_and_explicit_uncomputed_is_distinct():
    assert assess(regions()[:2]).status == "unknown"
    result = assess((), computed=False)
    assert result.status == "not_computed"
    with pytest.raises(ValueError):
        assess(regions(), computed=False)


def test_reference_only_success_does_not_certify_execution():
    passed = replace(assess(regions()), status="passed", violation_steps=0, peak_import_mw=12)
    summary = summarize_cluster_validation(
        (
            replace(passed, stage="centralized_reference"),
            replace(passed, stage="admm_reference"),
        ),
        scope="reference_only",
    )
    assert summary.assessed_scope_status == "passed"
    assert summary.execution_status == "not_computed"
    assert (
        summarize_cluster_validation(
            summary.stages, scope="hierarchical_execution"
        ).assessed_scope_status
        == "unknown"
    )


def test_tolerance_boundary_uses_mw_and_regions_need_not_share_container_order():
    a, b, c = regions()
    rows = tuple(replace(r, import_mw=(4.0, 4.0)) for r in (c, a, b))
    assert assess(rows).status == "passed"
    rows = (replace(rows[0], import_mw=(6.000005, 4.0)), *rows[1:])
    assert assess(rows).status == "passed"
    assert assess(rows, tolerance_mw=0.0).status == "violated"


@pytest.mark.parametrize("limit", [-1.0, float("nan"), float("inf")])
def test_invalid_limit_is_not_an_operating_result(limit):
    with pytest.raises(ValueError):
        assess_cluster_import(
            stage="minute", expected_regions=("a",), trajectories=(), limit_mw=limit
        )

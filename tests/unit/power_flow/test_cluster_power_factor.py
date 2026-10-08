"""Analytic aggregate PF witnesses, independent of coordinator/solver formulas."""

from dataclasses import replace
from math import inf, nan

import pytest

from oilfield_energy.modules.power_flow.api import assess_cluster_power_factor
from oilfield_energy.modules.power_flow.contracts import ClusterPowerFactorPolicy, RegionalPCCPower


def row(name="A", p=(3.0,), q=(1.0,), times=(0.0,)):
    return RegionalPCCPower(name, times, p, q, (True,) * len(times))


def assess(rows, target=0.95, names=("A",), **kwargs):
    return assess_cluster_power_factor(
        stage="day_ahead",
        expected_regions=names,
        trajectories=rows,
        policy=ClusterPowerFactorPolicy(target),
        **kwargs,
    )


def test_reference_pf_and_three_local_tracking_passes_are_insufficient():
    # 3-4-5 triangle: each reference is (4, 3), PF .8 exactly.
    # All actual errors fit .05, but aggregate PF is strictly below .8.
    names = ("A", "B", "C")
    reference = tuple(row(n, (4.0,), (3.0,)) for n in names)
    actual = tuple(row(n, (3.96,), (3.04,)) for n in names)
    assert assess(reference, 0.8, names).status == "passed"
    result = assess(actual, 0.8, names)
    assert result.status == "violated"
    assert result.points[0].power_factor < 0.8


def test_historical_aggregate_numbers_fail_095_but_pass_09():
    witness = (row(p=(23.84209850630681,), q=(7.934688909006507,)),)
    result = assess(witness)
    assert result.points[0].power_factor == pytest.approx(0.948834542953193)
    assert result.status == "violated"
    assert assess(witness, 0.9).status == "passed"


def test_signed_q_is_summed_before_magnitude_and_each_slot_matters():
    rows = (
        row("A", (3.0, 3.0), (4.0, 1.0), (0.0, 15.0)),
        row("B", (3.0, 3.0), (-4.0, 3.0), (0.0, 15.0)),
    )
    result = assess(rows, names=("A", "B"))
    assert [p.status for p in result.points] == ["passed", "violated"]
    assert result.points[0].power_factor == 1
    assert result.status == "violated"
    assert (
        assess(
            tuple(replace(r, q_mvar=tuple(-q for q in r.q_mvar)) for r in rows), names=("A", "B")
        ).status
        == "violated"
    )


@pytest.mark.parametrize(
    "p,q,status",
    [
        (0, 0, "unknown"),
        (0, 1, "violated"),
        (-1, 0, "violated"),
        (1e-200, 1e-200, "violated"),
        (3e307, 4e307, "violated"),
        (4, 3, "violated"),
        (4, -3, "violated"),
        (1, 0, "passed"),
    ],
)
def test_zero_reverse_near_zero_and_capacitive_points(p, q, status):
    assert assess((row(p=(p,), q=(q,)),)).status == status


@pytest.mark.parametrize(
    "rows,names",
    [
        ((), ("A",)),
        ((row(), row()), ("A", "B")),
        ((row(), row("B", times=(15.0,))), ("A", "B")),
        ((row(q=()),), ("A",)),
        ((row(times=(nan,)),), ("A",)),
        ((row(p=(nan,)),), ("A",)),
        ((row(q=(inf,)),), ("A",)),
        ((replace(row(), valid=(False,)),), ("A",)),
        ((row("A", p=(1e308,)), row("B", p=(1e308,))), ("A", "B")),
    ],
)
def test_missing_invalid_or_overflow_evidence_never_passes(rows, names):
    result = assess(rows, names=names)
    assert result.status == "unknown" and not result.points


@pytest.mark.parametrize("target", [0, -1, 1.01, inf, nan, True])
def test_invalid_target_rejected(target):
    with pytest.raises(ValueError):
        ClusterPowerFactorPolicy(target)


def test_not_computed_and_policy_tolerance_are_explicit():
    assert assess((), computed=False).status == "not_computed"
    with pytest.raises(ValueError):
        assess((row(),), computed=False)
    with pytest.raises(ValueError):
        ClusterPowerFactorPolicy(0.95, numerical_tolerance=0.95)

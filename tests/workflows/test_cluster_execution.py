from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from oilfield_energy.workflows.cluster_execution.api import verify_execution
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionCheck,
    ExecutionPolicy,
    RegionalExecution,
)


def test_historical_policy_does_not_invent_active_tracking_version():
    assert ExecutionPolicy.model_validate({}).active_tracking_version is None
    recorded = ExecutionPolicy(active_tracking_version="active-tracking-allocation-v1")
    assert ExecutionPolicy.model_validate_json(recorded.model_dump_json()) == recorded


def evidence(name, p=2.0, *, status="passed"):
    return RegionalExecution(
        region=name,
        status=status,
        checks=(ExecutionCheck(code="AC_NETWORK", label="AC", status=status),),
        time_minutes=(0.0, 15.0),
        p_mw=(p, p),
        q_mvar=(0.0, 0.0),
        p_target_mw=(2.0, 2.0),
        q_target_mvar=(0.0, 0.0),
        storage_power_mw=(0.0, 0.0),
        storage_energy_mwh=(2.0, 2.0),
    )


def run(runner, *, converged=True, limit=5.0):
    progress = Mock()
    result = verify_execution(
        names=("A", "B"),
        limit_mw=limit,
        converged=converged,
        storage_enabled=False,
        policy=ExecutionPolicy(),
        input_basis="test input",
        dataset_sha256=None,
        run_stage=runner,
        progress=progress,
    )
    assert progress.call_count == 3
    return result


def test_one_call_runs_all_stages_and_uses_realized_power_for_capacity():
    runner = Mock(
        side_effect=[
            (evidence("A"), evidence("B")),
            (evidence("A", 3), evidence("B", 3)),
            (evidence("A"), evidence("B")),
        ]
    )
    result = run(runner)
    assert [c.args[0] for c in runner.call_args_list] == [
        "day_ahead",
        "intraday",
        "minute",
    ]
    assert result.status == "violated"
    assert result.stages[1].cluster_import.peak_import_mw == 6
    assert result.stages[1].cluster_import.violation_steps == 2
    assert result.stages[1].regions[0].p_target_mw == (2, 2)


def test_unconverged_reference_never_reaches_execution_engines():
    runner = Mock(side_effect=AssertionError("must not execute"))
    result = run(runner, converged=False)
    runner.assert_not_called()
    assert result.status == "not_computed"
    assert all(
        s.reason and s.cluster_import.peak_import_mw is None for s in result.stages
    )


def test_unknown_or_missing_evidence_cannot_pass_or_become_zero():
    unavailable = RegionalExecution(
        region="B", status="unknown", reason="solver timeout"
    )
    result = run(lambda stage: (evidence("A"), unavailable))
    assert result.status == "unknown"
    assert all(s.cluster_import.status == "unknown" for s in result.stages)
    assert all(s.cluster_import.peak_import_mw is None for s in result.stages)
    with pytest.raises(ValueError, match="exactly once"):
        run(lambda stage: (evidence("A"), evidence("A")))


def test_tracking_failure_is_not_overridden_by_network_or_import_pass():
    region = evidence("A").model_copy(
        update={
            "status": "violated",
            "checks": (
                ExecutionCheck(
                    code="P_TRACKING",
                    label="P",
                    status="violated",
                    actual=0.1,
                    limit=0.05,
                    unit="MW",
                ),
            ),
        }
    )
    result = run(lambda stage: (region, evidence("B")))
    assert result.status == "violated"
    assert all(s.cluster_import.status == "passed" for s in result.stages)


def test_contract_rejects_false_pass_nonfinite_and_misaligned_evidence():
    with pytest.raises(ValidationError):
        RegionalExecution(region="A", status="passed")
    for update in (
        {"p_mw": [float("nan"), 2]},
        {"q_mvar": [0]},
        {"time_minutes": [0, 0]},
    ):
        with pytest.raises(ValidationError):
            RegionalExecution.model_validate({**evidence("A").model_dump(), **update})
    with pytest.raises(ValidationError):
        ExecutionPolicy(p_tracking_tolerance_mw=0)

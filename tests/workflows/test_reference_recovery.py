"""Causal recovery, no solver and no substitute physical feasibility claims."""

from dataclasses import FrozenInstanceError

import pytest

from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    CoordinationRecoveryPolicy,
)
from oilfield_energy.modules.power_flow.api import assess_cluster_import
from oilfield_energy.modules.power_flow.contracts import RegionalImport
from oilfield_energy.workflows.cluster_execution.api import RecoveryTrial, run_reference_recovery
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    CoordinationRecoveryRound,
    ExecutionCheck,
    ExecutionPolicy,
    RegionalCalibrationFeedback,
    RegionalExecution,
)


def calibration(loss):
    return CoordinationNetworkCalibration((loss,), (2 * loss,))


def witness(name="A", *, time=0.0, status="passed"):
    return RegionalExecution(
        region=name,
        status=status,
        checks=tuple(
            ExecutionCheck(code=c, label=c, status=status) for c in ("DEVICE_PLAN", "AC_NETWORK")
        ),
        time_minutes=(time,),
        p_mw=(2.0,),
        q_mvar=(0.2,),
        p_target_mw=(1.0,),
        q_target_mvar=(0.1,),
        storage_power_mw=(0.0,),
    )


def feedback(number, loss=0.2, **kwargs):
    row = witness(**kwargs)
    return (
        RegionalCalibrationFeedback(
            reference_round=number,
            evidence=row,
            calibration=calibration(loss) if row.status == "passed" else None,
        ),
    )


def run(attempt, diagnose, **kwargs):
    return run_reference_recovery(
        names=("A",),
        time_minutes=(0.0,),
        initial_calibration={"A": calibration(0.1)},
        policy=CoordinationRecoveryPolicy(max_recoordinations=2),
        attempt=lambda c, n, _margin: attempt(c, n),
        diagnose=diagnose,
        **kwargs,
    )


def rejected(_current, number):
    return RecoveryTrial(
        number, CoordinationRecoveryRound(round=number, outcome="regional_rejected")
    )


@pytest.mark.parametrize(
    "outcome", ["coordination_failed", "regional_unresolved", "joint_rejected", "error"]
)
def test_non_reference_failures_do_not_trigger_soft_diagnostic(outcome):
    def forbidden(*_):
        pytest.fail("ineligible failure must not enter recovery")

    result = run(
        lambda c, n: RecoveryTrial(n, CoordinationRecoveryRound(round=n, outcome=outcome)),
        forbidden,
    )
    assert result.report.stop_reason == outcome
    assert not result.report.accepted
    assert len(result.report.rounds) == 1


def test_adoption_requires_a_new_strict_trial_after_feedback():
    events = []

    def attempt(current, number):
        events.append(("strict", number, current["A"]))
        with pytest.raises(TypeError):
            current["A"] = calibration(9)
        with pytest.raises(FrozenInstanceError):
            current["A"].p_loss_mw = (9,)
        row = witness().model_copy(
            update={
                "checks": tuple(
                    ExecutionCheck(code=c, label=c, status="passed")
                    for c in (
                        "DEVICE_PLAN",
                        "AC_NETWORK",
                        "P_TRACKING",
                        "Q_TRACKING",
                    )
                ),
            }
        )
        return RecoveryTrial(
            number,
            CoordinationRecoveryRound(
                round=number,
                outcome="passed" if number == 2 else "regional_rejected",
                regions=(row,),
                p_target_mw={"A": row.p_target_mw},
                q_target_mvar={"A": row.q_target_mvar},
                cluster_import=assess_cluster_import(
                    stage="intraday",
                    expected_regions=("A",),
                    limit_mw=3,
                    trajectories=(RegionalImport("A", row.time_minutes, row.p_mw, (True,)),),
                ),
            ),
        )

    def diagnose(payload, number):
        assert payload == number
        events.append(("diagnostic", number))
        return feedback(number)

    result = run(attempt, diagnose)
    assert result.report.accepted and result.payload == 2
    assert [e[:2] for e in events] == [("strict", 1), ("diagnostic", 1), ("strict", 2)]
    assert events[-1][2] == calibration(0.2)
    assert result.report.rounds[0].calibration_used == {"A": calibration(0.1)}
    assert (
        CoordinationRecoveryReport.model_validate_json(result.report.model_dump_json())
        == result.report
    )


@pytest.mark.parametrize(
    "kind", ["stale_round", "wrong_region", "wrong_axis", "missing_region", "duplicate"]
)
def test_feedback_cannot_cross_round_region_or_time_axis(kind):
    values = {
        "stale_round": feedback(2),
        "wrong_region": feedback(1, name="B"),
        "wrong_axis": feedback(1, time=15),
        "missing_region": (),
        "duplicate": feedback(1) * 2,
    }
    result = run(rejected, lambda *_: values[kind])
    assert result.report.stop_reason == "error"
    assert not result.report.accepted and len(result.report.rounds) == 1


@pytest.mark.parametrize("status", ["violated", "unknown"])
def test_no_calibration_from_failed_or_unknown_diagnostic(status):
    result = run(rejected, lambda _, n: feedback(n, status=status))
    assert result.report.stop_reason == "feedback_unavailable"
    assert len(result.report.rounds) == 1
    assert result.report.rounds[0].feedback[0].evidence.status == status
    with pytest.raises(ValueError, match="physical witness"):
        RegionalCalibrationFeedback(
            reference_round=1, evidence=witness(status=status), calibration=calibration(0.2)
        )


@pytest.mark.parametrize("values,expected_rounds", [([0.1], 1), ([0.2, 0.1], 2), ([0.2, 0.2], 2)])
def test_unchanged_and_cyclic_effective_calibrations_stop(values, expected_rounds):
    result = run(rejected, lambda _, n: feedback(n, values[n - 1]))
    assert result.report.stop_reason == "stagnated"
    assert len(result.report.rounds) == expected_rounds
    assert not result.report.accepted


def test_budget_counts_recoordinations_and_does_not_diagnose_after_last_trial():
    calls = []

    def diagnose(_, number):
        calls.append(number)
        return feedback(number, 0.1 + 0.1 * number)

    result = run(rejected, diagnose)
    assert result.report.stop_reason == "budget_exhausted"
    assert len(result.report.rounds) == 3 and calls == [1, 2]
    assert not result.report.rounds[-1].feedback


@pytest.mark.parametrize("where", ["coordinate", "diagnose"])
def test_expected_numerical_error_preserves_prior_attempts(where):
    def attempt(current, number):
        if where == "coordinate" and number == 2:
            raise RuntimeError("numerical engine stopped")
        return rejected(current, number)

    def diagnose(_, number):
        if where == "diagnose":
            raise ValueError("invalid solver outputs")
        return feedback(number)

    result = run(attempt, diagnose)
    assert result.report.stop_reason == "error" and not result.report.accepted
    assert result.report.rounds[0].outcome == "regional_rejected"
    assert len(result.report.rounds) == (2 if where == "coordinate" else 1)


def test_historical_policy_has_no_recovery_claim():
    assert ExecutionPolicy.model_validate({}).coordination_recovery is None
    with pytest.raises(ValueError):
        CoordinationRecoveryPolicy(max_recoordinations=True)
    with pytest.raises(ValueError):
        CoordinationNetworkCalibration((0,), (0,), version="unknown")


def test_accepted_history_requires_regional_and_joint_certificates():
    with pytest.raises(ValueError, match="joint certificates"):
        CoordinationRecoveryRound(round=1, outcome="passed")


def test_diagnostic_cannot_carry_an_adoption_certificate():
    row = witness()
    row = row.model_copy(
        update={
            "checks": (*row.checks, ExecutionCheck(code="P_TRACKING", label="P", status="passed"))
        }
    )
    with pytest.raises(ValueError, match="target adoption"):
        RegionalCalibrationFeedback(reference_round=1, evidence=row, calibration=calibration(0.2))

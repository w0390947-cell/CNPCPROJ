"""Candidate provenance, strict re-realization and failure semantics."""

import json

import pytest

from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    CoordinationRecoveryPolicy,
    NetworkCandidatePolicy,
    PCCTrackingLimits,
)
from oilfield_energy.modules.power_flow.api import (
    assess_cluster_import,
    assess_cluster_power_factor,
)
from oilfield_energy.modules.power_flow.contracts import (
    ClusterPowerFactorPolicy,
    RegionalImport,
    RegionalPCCPower,
)
from oilfield_energy.workflows.cluster_execution.api import RecoveryTrial, run_reference_recovery
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    CoordinationRecoveryRound,
    ExecutionCheck,
    RegionalCalibrationFeedback,
    RegionalExecution,
)

NAMES = ("A", "B", "C")
CAL = CoordinationNetworkCalibration((0.1,), (0.1,))
PF = ClusterPowerFactorPolicy(0.95)


def diagnostic(number, *, power=4.0, status="passed"):
    return tuple(
        RegionalCalibrationFeedback(
            reference_round=number,
            calibration=CAL if status == "passed" else None,
            evidence=RegionalExecution(
                region=n,
                status=status,
                time_minutes=(0.0,),
                p_mw=(power,),
                q_mvar=(0.1,),
                p_target_mw=(1.0,),
                q_target_mvar=(0.1,),
                storage_power_mw=(0.0,),
                checks=tuple(
                    ExecutionCheck(code=c, label=c, status=status)
                    for c in ("DEVICE_PLAN", "AC_NETWORK")
                ),
            ),
        )
        for n in NAMES
    )


def rejected(_cal, number, _margin):
    return RecoveryTrial(
        number,
        CoordinationRecoveryRound(
            round=number,
            outcome="regional_rejected",
            p_target_mw={n: (1.0,) for n in NAMES},
            q_target_mvar={n: (0.1,) for n in NAMES},
        ),
    )


def accepted(_cal, number, _margin, candidates):
    rows = tuple(
        RegionalExecution(
            region=c.region,
            status="passed",
            time_minutes=c.time_minutes,
            p_mw=c.p_mw,
            q_mvar=c.q_mvar,
            p_target_mw=c.p_mw,
            q_target_mvar=c.q_mvar,
            storage_power_mw=(0.0,),
            checks=tuple(
                ExecutionCheck(code=x, label=x, status="passed")
                for x in ("DEVICE_PLAN", "AC_NETWORK", "P_TRACKING", "Q_TRACKING")
            ),
        )
        for c in candidates
    )
    return RecoveryTrial(
        number,
        CoordinationRecoveryRound(
            round=number,
            outcome="passed",
            regions=rows,
            p_target_mw={r.region: r.p_mw for r in rows},
            q_target_mvar={r.region: r.q_mvar for r in rows},
            cluster_import=assess_cluster_import(
                stage="intraday",
                expected_regions=NAMES,
                limit_mw=15.0,
                trajectories=tuple(
                    RegionalImport(r.region, r.time_minutes, r.p_mw, (True,)) for r in rows
                ),
            ),
            cluster_power_factor=assess_cluster_power_factor(
                stage="intraday",
                expected_regions=NAMES,
                policy=PF,
                trajectories=tuple(
                    RegionalPCCPower(r.region, r.time_minutes, r.p_mw, r.q_mvar, (True,))
                    for r in rows
                ),
            ),
        ),
    )


def run(*, diagnostics=lambda _, n: diagnostic(n), candidate_attempt=accepted, retries=3):
    return run_reference_recovery(
        names=NAMES,
        time_minutes=(0.0,),
        initial_calibration={n: CAL for n in NAMES},
        policy=CoordinationRecoveryPolicy(max_recoordinations=retries),
        attempt=rejected,
        diagnose=diagnostics,
        power_factor_policy=PF,
        tracking_limits=PCCTrackingLimits(0.05, 0.05, 1e-6),
        network_candidate_policy=NetworkCandidatePolicy(),
        import_limit_mw=15.0,
        attempt_candidates=candidate_attempt,
    )


def test_unchanged_losses_are_not_stagnation_when_a_new_joint_witness_exists():
    calls = []

    def strict(cal, number, margin, candidates):
        calls.append((number, tuple(c.region for c in candidates)))
        assert all(c.calibration == cal[c.region] for c in candidates)
        return accepted(cal, number, margin, candidates)

    result = run(candidate_attempt=strict)
    assert result.report.accepted and calls == [(2, NAMES)]
    assert result.report.rounds[1].network_candidates_used
    assert all(not f.evidence.executable_plan for f in result.report.rounds[0].feedback)


def test_rejected_strict_realization_cannot_adopt_diagnostic_or_retry_same_point():
    calls = []

    def fail(cal, number, margin, candidates):
        calls.append(number)
        return rejected(cal, number, margin)

    result = run(candidate_attempt=fail)
    assert not result.report.accepted and result.report.stop_reason == "stagnated"
    assert calls == [2] and len(result.report.rounds) == 2
    assert result.report.rounds[-1].candidate_selection.status == "no_joint_candidate"


def test_local_witnesses_cannot_bypass_joint_capacity():
    def forbidden(*_):
        pytest.fail("no joint combination may enter coordination")

    result = run(diagnostics=lambda _, n: diagnostic(n, power=6.0), candidate_attempt=forbidden)
    assert not result.report.accepted
    assert result.report.rounds[0].candidate_selection.status == "no_joint_candidate"


@pytest.mark.parametrize("status", ["unknown", "violated"])
def test_uncertified_diagnostic_never_enters_candidate_pool(status):
    def forbidden(*_):
        pytest.fail("untrusted witness must not be coordinated")

    result = run(diagnostics=lambda _, n: diagnostic(n, status=status), candidate_attempt=forbidden)
    assert result.report.stop_reason == "feedback_unavailable"
    assert result.report.rounds[0].candidate_selection is None


def test_zero_recoordination_budget_never_searches_candidates():
    def forbidden(*_):
        pytest.fail("exhausted budget must not generate candidates")

    result = run(retries=0, diagnostics=forbidden, candidate_attempt=forbidden)
    assert result.report.stop_reason == "budget_exhausted" and len(result.report.rounds) == 1


@pytest.mark.parametrize(
    "field,value", [("p_mw", [3.0]), ("source_round", 2), ("region", "unknown")]
)
def test_serialized_candidate_must_match_its_recorded_physical_witness(field, value):
    report = run().report
    data = json.loads(report.model_dump_json())
    data["rounds"][1]["network_candidates_used"][0][field] = value
    with pytest.raises(ValueError, match="witness|selection"):
        CoordinationRecoveryReport.model_validate(data)


def test_old_records_do_not_gain_candidate_policy():
    data = run().report.model_dump()
    data.pop("network_candidate_policy")
    with pytest.raises(ValueError, match="explicit policy"):
        CoordinationRecoveryReport.model_validate(data)


def test_candidate_attempt_cannot_use_a_different_witness_calibration():
    data = json.loads(run().report.model_dump_json())
    data["rounds"][1]["calibration_used"]["A"]["p_loss_mw"] = [0.2]
    with pytest.raises(ValueError, match="own calibration"):
        CoordinationRecoveryReport.model_validate(data)


def test_candidate_source_round_must_match_the_diagnostic_reference_round():
    data = json.loads(run().report.model_dump_json())
    data["rounds"][0]["feedback"][0]["reference_round"] = 2
    with pytest.raises(ValueError, match="physical witness"):
        CoordinationRecoveryReport.model_validate(data)


def test_candidate_search_cannot_exceed_its_recorded_policy_budget():
    data = json.loads(run().report.model_dump_json())
    selection = data["rounds"][0]["candidate_selection"]
    selection["combinations_checked"] = selection["total_combinations"] = 4097
    with pytest.raises(ValueError, match="policy budget"):
        CoordinationRecoveryReport.model_validate(data)


def test_candidate_solver_error_retains_provenance_without_adoption():
    def fail(*_):
        raise RuntimeError("candidate coordinator failed")

    result = run(candidate_attempt=fail)
    assert result.report.stop_reason == "error" and not result.report.accepted
    assert result.report.rounds[-1].network_candidates_used
    assert not result.report.rounds[-1].regions

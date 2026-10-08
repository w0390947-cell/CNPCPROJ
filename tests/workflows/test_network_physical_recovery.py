"""Joint restoration cannot bypass failed communication or adoption evidence."""

import pytest

from oilfield_energy.modules.dispatch.contracts import (
    CoordinationRecoveryPolicy,
    JointNetworkEvidence,
    NetworkCoordinationPolicy,
    PCCTrackingLimits,
    RegionalNetworkCandidate,
)
from oilfield_energy.workflows.cluster_execution.api import (
    RecoveryTrial,
    run_reference_recovery,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    CoordinationRecoveryRound,
)
from tests.workflows.test_network_candidate_recovery import (
    CAL,
    NAMES,
    PF,
    accepted,
    diagnostic,
    rejected,
)


def joint(_payload, number, margin):
    points = tuple(
        RegionalNetworkCandidate(
            region=n,
            source_round=1,
            time_minutes=(0.0,),
            p_mw=(4.0,),
            q_mvar=(0.1,),
            calibration=CAL,
        )
        for n in NAMES
    )
    row = accepted(None, number, margin, points).evidence
    evidence = JointNetworkEvidence(
        status="certified",
        time_limit_seconds=10,
        elapsed_seconds=0.1,
        source_p_mw={n: (1.0,) for n in NAMES},
        source_q_mvar={n: (0.1,) for n in NAMES},
        time_minutes=(0.0,),
        pf_margin_mvar=margin,
        deviation=180,
        ac_iterations=1,
        storage_integrality_certified=True,
        p_mw=row.p_target_mw,
        q_mvar=row.q_target_mvar,
    )
    return RecoveryTrial(
        number,
        CoordinationRecoveryRound.model_validate(
            {
                **row.model_dump(),
                "reference_method": "joint-network",
                "joint_network": evidence,
            }
        ),
    )


def run(attempt=rejected, joint_attempt=joint):
    return run_reference_recovery(
        names=NAMES,
        time_minutes=(0.0,),
        initial_calibration={n: CAL for n in NAMES},
        policy=CoordinationRecoveryPolicy(),
        attempt=attempt,
        diagnose=lambda _, n: diagnostic(n),
        power_factor_policy=PF,
        tracking_limits=PCCTrackingLimits(0.05, 0.05, 1e-6),
        network_coordination_policy=NetworkCoordinationPolicy(
            joint_time_limit_seconds=10
        ),
        attempt_joint=joint_attempt,
    )


def test_joint_restoration_progresses_after_calibration_stagnation_and_round_trips():
    result = run()
    assert result.report.accepted and len(result.report.rounds) == 2
    assert result.report.rounds[-1].admm_iterations == 0
    assert (
        CoordinationRecoveryReport.model_validate_json(result.report.model_dump_json())
        == result.report
    )


def test_failed_communication_never_uses_joint_solver_as_execution_authority():
    def failure(_, n, _margin):
        return RecoveryTrial(
            n, CoordinationRecoveryRound(round=n, outcome="coordination_failed")
        )

    def forbidden(*_):
        pytest.fail("incomplete coordinated window must not enter joint recovery")

    result = run(attempt=failure, joint_attempt=forbidden)
    assert (
        not result.report.accepted
        and result.report.stop_reason == "coordination_failed"
    )


def test_timeout_or_unknown_joint_solution_remains_rejected_without_retry_loop():
    calls = []

    def unknown(payload, n, margin):
        calls.append(n)
        row = joint(payload, n, margin).evidence.model_dump()
        row.update(
            outcome="regional_unresolved",
            regions=[],
            cluster_import=None,
            cluster_power_factor=None,
            p_target_mw={n: (1.0,) for n in NAMES},
            q_target_mvar={n: (0.1,) for n in NAMES},
        )
        row["joint_network"].update(
            status="unresolved",
            reason="time limit without certified solution",
            p_mw={},
            q_mvar={},
        )
        return RecoveryTrial(payload, CoordinationRecoveryRound.model_validate(row))

    result = run(joint_attempt=unknown)
    assert calls == [2] and not result.report.accepted


@pytest.mark.parametrize("tamper", ["source", "reference", "method", "budget"])
def test_restoration_evidence_cannot_be_rebound_or_relabelled(tamper):
    data = run().report.model_dump()
    row = data["rounds"][-1]
    if tamper == "source":
        row["joint_network"]["source_p_mw"]["A"] = (99.0,)
    elif tamper == "reference":
        row["joint_network"]["p_mw"]["A"] = (99.0,)
    elif tamper == "method":
        row["admm_iterations"] = 5
    else:
        data["network_coordination_policy"] = None
    with pytest.raises(ValueError):
        CoordinationRecoveryReport.model_validate(data)

"""Joint PF feedback, evidence, budgets and historical compatibility."""

from dataclasses import replace

import pytest

from oilfield_energy.modules.dispatch.contracts import CoordinationRecoveryPolicy, PCCTrackingLimits
from oilfield_energy.modules.power_flow.api import (
    assess_cluster_import,
    assess_cluster_power_factor,
)
from oilfield_energy.modules.power_flow.contracts import (
    ClusterPowerFactorPolicy,
    RegionalImport,
    RegionalPCCPower,
)
from oilfield_energy.workflows.cluster_execution.api import (
    RecoveryTrial,
    run_reference_recovery,
    verify_execution,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    CoordinationRecoveryRound,
    ExecutionCheck,
    ExecutionPolicy,
    RegionalExecution,
)

NAMES = ("A", "B", "C")
TIMES = (0.0, 15.0)
PF = ClusterPowerFactorPolicy(0.8)
LIMITS = PCCTrackingLimits(0.05, 0.05, 1e-6)


def trial(number, margin, *, ignore_margin=False):
    used = (0.0, 0.0) if ignore_margin else margin
    q_ref = (0.0, 3.0 - used[1] / 3)
    rows = tuple(
        RegionalExecution(
            region=n,
            status="passed",
            time_minutes=TIMES,
            p_mw=(4.0, 4.0),
            q_mvar=(0.0, q_ref[1] + 0.04),
            p_target_mw=(4.0, 4.0),
            q_target_mvar=q_ref,
            storage_power_mw=(0.0, 0.0),
            checks=tuple(
                ExecutionCheck(code=c, label=c, status="passed")
                for c in ("DEVICE_PLAN", "AC_NETWORK", "P_TRACKING", "Q_TRACKING")
            ),
        )
        for n in NAMES
    )
    pf = assess_cluster_power_factor(
        stage="intraday",
        expected_regions=NAMES,
        policy=PF,
        trajectories=tuple(
            RegionalPCCPower(r.region, TIMES, r.p_mw, r.q_mvar, (True, True)) for r in rows
        ),
    )
    cap = assess_cluster_import(
        stage="intraday",
        expected_regions=NAMES,
        limit_mw=15,
        trajectories=tuple(RegionalImport(r.region, TIMES, r.p_mw, (True, True)) for r in rows),
    )
    return RecoveryTrial(
        rows,
        CoordinationRecoveryRound(
            round=number,
            outcome="passed" if pf.status == "passed" else "power_factor_rejected",
            regions=rows,
            p_target_mw={r.region: r.p_target_mw for r in rows},
            q_target_mvar={r.region: r.q_target_mvar for r in rows},
            cluster_import=cap,
            cluster_power_factor=pf,
        ),
    )


def run(*, budget=3, ignore=False, policy_enabled=True):
    def forbidden(*_):
        pytest.fail("joint PF failure must not invoke a soft regional diagnostic")

    return run_reference_recovery(
        names=NAMES,
        time_minutes=TIMES,
        initial_calibration=None,
        policy=CoordinationRecoveryPolicy(max_recoordinations=budget) if policy_enabled else None,
        power_factor_policy=PF,
        tracking_limits=LIMITS,
        attempt=lambda c, n, m: trial(n, m, ignore_margin=ignore),
        diagnose=forbidden,
    )


def test_later_horizon_slot_blocks_adoption_and_only_failed_slot_is_tightened():
    result = run()
    assert result.report.accepted and len(result.report.rounds) == 2
    first, second = result.report.rounds
    assert [p.status for p in first.cluster_power_factor.points] == ["passed", "violated"]
    assert second.power_factor_margin_mvar[0] == 0
    assert second.power_factor_margin_mvar[1] > 0.12
    assert all(r.executable_plan for r in result.payload)
    assert second.cluster_power_factor.status == "passed"
    assert (
        CoordinationRecoveryReport.model_validate_json(result.report.model_dump_json())
        == result.report
    )


@pytest.mark.parametrize(
    "budget,stop,count",
    [(0, "budget_exhausted", 1), (1, "budget_exhausted", 2), (6, "stagnated", 4)],
)
def test_ignored_reference_feedback_never_bypasses_gate(budget, stop, count):
    result = run(budget=budget, ignore=True)
    assert not result.report.accepted and result.report.stop_reason == stop
    assert len(result.report.rounds) == count
    assert all(r.cluster_power_factor.status == "violated" for r in result.report.rounds)


def test_disabled_recovery_still_rejects_bad_joint_plan():
    result = run(policy_enabled=False)
    assert not result.report.accepted and result.report.stop_reason == "disabled"


def test_new_report_requires_pf_evidence_but_does_not_invent_it_in_history():
    data = run().report.model_dump()
    data["rounds"][-1]["cluster_power_factor"] = None
    with pytest.raises(ValueError, match="power factor certificate"):
        CoordinationRecoveryReport.model_validate(data)
    data.pop("power_factor_policy")
    old = CoordinationRecoveryReport.model_validate(data)
    assert old.power_factor_policy is None and old.rounds[-1].cluster_power_factor is None
    assert ExecutionPolicy.model_validate({}).cluster_power_factor is None


def test_full_stage_verification_checks_plans_without_claiming_minute_pf():
    rows = trial(1, (0.0, 0.0)).payload
    result = verify_execution(
        names=NAMES,
        limit_mw=15,
        converged=True,
        storage_enabled=False,
        policy=ExecutionPolicy(cluster_power_factor=PF),
        input_basis="synthetic",
        dataset_sha256=None,
        run_stage=lambda _: rows,
        progress=lambda _: None,
    )
    assert result.stages[0].status == "violated"
    assert result.stages[1].status == "violated"
    assert result.stages[2].cluster_power_factor is None


@pytest.mark.parametrize("bad", ["missing", "policy", "axis"])
def test_inconsistent_pf_evidence_stops_without_retry(bad):
    def invalid(c, n, m):
        value = trial(n, m)
        pf = value.evidence.cluster_power_factor
        if bad == "policy":
            pf = replace(pf, policy=ClusterPowerFactorPolicy(0.9))
        elif bad == "axis":
            pf = replace(pf, points=pf.points[:1])
        else:
            pf = None
        return replace(
            value, evidence=value.evidence.model_copy(update={"cluster_power_factor": pf})
        )

    result = run_reference_recovery(
        names=NAMES,
        time_minutes=TIMES,
        initial_calibration=None,
        policy=CoordinationRecoveryPolicy(),
        power_factor_policy=PF,
        tracking_limits=LIMITS,
        attempt=invalid,
        diagnose=None,
    )
    assert not result.report.accepted and result.report.stop_reason == "error"
    assert len(result.report.rounds) == 1

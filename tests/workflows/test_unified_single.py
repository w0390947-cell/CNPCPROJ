"""Safety induction, finite storage, future availability, and honest tracking."""

import pytest

from oilfield_energy.modules.control.contracts import (
    TubeCommand,
    TubeDeviceModel,
    TubeResource,
    TubeState,
)
from oilfield_energy.modules.power_flow.contracts import EnvelopeBranch, RadialEnvelope
from oilfield_energy.modules.power_flow.contracts import PowerInterval as I
from oilfield_energy.workflows.single_execution.api import (
    admit_tracking_target,
    certify_tracking_target,
    UnifiedSingleController,
    verify_unified_execution,
)
from oilfield_energy.workflows.single_execution.contracts import (
    BoundedBusLoad,
    FirmGeneration,
    SingleForecastEnvelope,
    SingleResearchPolicy,
    UnifiedDomain,
    UnifiedExecution,
)


def domain(*, sunset=None):
    network = RadialEnvelope(
        network_id="analytic",
        buses=("pcc", "load"),
        branches=(
            EnvelopeBranch(
                parent=0, child=1, r_pu=0.01, x_pu=0.02, tap=1, rating_mva=10
            ),
        ),
        slack=0,
        base_mva=10,
        slack_voltage_pu=1,
        shunt_q_mvar=(0, 0),
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        pcc_min_mw=0.1,
        pcc_max_mw=5,
        pf_min=0.9,
    )
    resources = (
        TubeResource(
            resource_id="wind",
            bus="load",
            kind="wind",
            p=I(low=0, high=1.5),
            q=I(low=-0.5, high=0.5),
            s_mva=2,
            q_over_p=0.3,
            alpha_p=0.5,
            ramp_p_mw=0.4,
        ),
        TubeResource(
            resource_id="pv",
            bus="load",
            kind="pv",
            p=I(low=0, high=1),
            q=I(low=0, high=0),
            s_mva=1.1,
            alpha_p=0.98,
            ramp_p_mw=0.2,
        ),
        TubeResource(
            resource_id="pcs",
            bus="load",
            kind="storage",
            p=I(low=-0.5, high=0.5),
            q=I(low=0, high=0),
            s_mva=1,
            alpha_p=1,
            ramp_p_mw=0.4,
        ),
    )
    devices = TubeDeviceModel(
        version="unified-device-v1",
        resources=resources,
        alpha_q=0.5,
        aggregate_q_ramp_mvar=0.9,
        step_minutes=1,
        energy_mwh=I(low=0.2, high=2),
        eta_charge=0.95,
        eta_discharge=0.95,
        context_id="analytic",
        settling_resolution=0.0001,
    )
    forecasts = tuple(
        SingleForecastEnvelope(
            loads=(
                BoundedBusLoad(
                    bus="pcc", p_mw=I(low=0, high=0), q_mvar=I(low=0, high=0)
                ),
                BoundedBusLoad(
                    bus="load", p_mw=I(low=2.99, high=3.01), q_mvar=I(low=0, high=0)
                ),
            ),
            generation=(
                FirmGeneration(resource_id="wind", minimum_available_mw=1.5),
                FirmGeneration(
                    resource_id="pv",
                    minimum_available_mw=0 if sunset is not None and k >= sunset else 1,
                ),
            ),
        )
        for k in range(30)
    )
    return UnifiedDomain(
        research=SingleResearchPolicy(),
        network=network,
        devices=devices,
        forecasts=forecasts,
        terminal_command=TubeCommand(p_mw=(0, 0, 0), q_mvar=(0, 0, 0)),
        p_band_mw=0.05,
        q_band_mvar=0.05,
        response_steps=10,
        context_id="analytic",
    )


def observation(d, step):
    f = d.forecasts[min(step, len(d.forecasts) - 1)]
    return f.model_copy(
        update={
            "loads": tuple(
                b.model_copy(
                    update={
                        "p_mw": I(
                            low=(b.p_mw.low + b.p_mw.high) / 2,
                            high=(b.p_mw.low + b.p_mw.high) / 2,
                        )
                    }
                )
                for b in f.loads
            )
        }
    )


def initial():
    return TubeState(p_mw=(0, 0, 0), q_mvar=(0, 0, 0), energy_mwh=1)


def tracking_domain():
    d = domain()
    svg = TubeResource(
        resource_id="svg",
        bus="load",
        kind="svg",
        p=I(low=0, high=0),
        q=I(low=-1, high=1),
        s_mva=1,
        alpha_p=1,
        ramp_p_mw=0,
    )
    devices = d.devices.model_copy(update={"resources": (*d.devices.resources, svg)})
    terminal = TubeCommand(p_mw=(0, 0, 0, 0), q_mvar=(0, 0, 0, 0.2))
    forecasts = tuple(
        f.model_copy(
            update={
                "loads": (
                    f.loads[0],
                    f.loads[1].model_copy(update={"q_mvar": I(low=0.29, high=0.31)}),
                )
            }
        )
        for f in d.forecasts
    )
    return d.model_copy(
        update={
            "devices": devices,
            "forecasts": forecasts,
            "terminal_command": terminal,
        }
    )


def test_admitted_feedback_tracks_alternating_disturbance_corners_with_analytic_ac():
    from math import sqrt

    d = tracking_domain()
    state = TubeState(p_mw=(0, 0, 0, 0), q_mvar=(0, 0, 0, 0.2), energy_mwh=1)
    nominal = TubeCommand(p_mw=(1, 0.4, 0, 0), q_mvar=(0, 0, 0, 0.2))
    admission = admit_tracking_target(
        d, state, 0, 15, (1.6, 0.1), nominal, "external", None, 10
    )
    assert admission.accepted and admission.attempted_targets == ((1.6, 0.1),)
    plan = admission.plan
    assert plan is not None and plan.tracking is not None
    c = UnifiedSingleController(d, state)
    for k in range(15):
        p, q = (2.99, 0.31) if k % 2 else (3.01, 0.29)
        f = d.forecasts[k]
        observed = f.model_copy(
            update={
                "loads": (
                    f.loads[0],
                    f.loads[1].model_copy(
                        update={"p_mw": I(low=p, high=p), "q_mvar": I(low=q, high=q)}
                    ),
                )
            }
        )
        row = c.advance(
            observed,
            context_id=d.context_id,
            target=(1.6, 0.1),
            proposal=None,
            target_plan=plan if k == 0 else None,
            target_changed=k == 0,
        )
        # High-voltage DistFlow quadratic, independent of both feedback and certificate kernels.
        dp, dq = (p - sum(row.after.p_mw)) / 10, (q - sum(row.after.q_mvar)) / 10
        a = 1 - 2 * (0.01 * dp + 0.02 * dq)
        v2 = (a + sqrt(a * a - 4 * (0.01**2 + 0.02**2) * (dp * dp + dq * dq))) / 2
        ell = (dp * dp + dq * dq) / v2
        ac_p, ac_q = 10 * (dp + 0.01 * ell), 10 * (dq + 0.02 * ell)
        assert row.pcc_p_mw.contains(ac_p) and row.pcc_q_mvar.contains(ac_q)
        for phase in (0.001, 0.25, 0.5, 0.75, 0.999):
            # Continuous first-order paths before the finite endpoint correction;
            # the PCS uses zero-order hold immediately after command acceptance.
            rp = tuple(
                u if r.alpha_p == 1 else x + (1 - (1 - r.alpha_p) ** phase) * (u - x)
                for r, x, u in zip(
                    d.devices.resources, row.before.p_mw, row.command.p_mw
                )
            )
            rq = tuple(
                x + (1 - (1 - d.devices.alpha_q) ** phase) * (u - x)
                for x, u in zip(row.before.q_mvar, row.command.q_mvar)
            )
            pp, qq = (p - sum(rp)) / 10, (q - sum(rq)) / 10
            aa = 1 - 2 * (0.01 * pp + 0.02 * qq)
            vv = (
                aa + sqrt(aa * aa - 4 * (0.01**2 + 0.02**2) * (pp * pp + qq * qq))
            ) / 2
            ii = (pp * pp + qq * qq) / vv
            sp, sq = 10 * (pp + 0.01 * ii), 10 * (qq + 0.02 * ii)
            assert 0.95 <= sqrt(vv) <= 1.05
            assert 0.1 <= sp <= 5
            assert sp / sqrt(sp * sp + sq * sq) >= 0.9
            assert sqrt(sp * sp + sq * sq) <= 10
        if k + 1 >= 10:
            assert row.tracking_certified
            assert abs(ac_p - 1.6) <= 0.05 and abs(ac_q - 0.1) <= 0.05
    same = certify_tracking_target(
        d, plan.tracking.tubes[-1], 15, 30, 10, (1.6, 0.1), nominal
    )
    assert same.tracking.deadline_step == 10
    # Certificate expiry does not reset the target or hide the failed old obligation.
    for k in range(15, 30):
        f = d.forecasts[k]
        observed = f.model_copy(
            update={
                "loads": tuple(
                    b.model_copy(
                        update={
                            "p_mw": I(
                                low=(b.p_mw.low + b.p_mw.high) / 2,
                                high=(b.p_mw.low + b.p_mw.high) / 2,
                            ),
                            "q_mvar": I(
                                low=(b.q_mvar.low + b.q_mvar.high) / 2,
                                high=(b.q_mvar.low + b.q_mvar.high) / 2,
                            ),
                        }
                    )
                    for b in f.loads
                )
            }
        )
        row = c.advance(
            observed,
            context_id=d.context_id,
            target=(1.6, 0.1),
            proposal=None,
            proposal_failure="update timeout",
        )
        assert row.deadline_step == 10 and row.requested_p_mw == 1.6
    assert not row.tracking_certified


def test_external_unreachable_target_is_rejected_and_cannot_be_silently_rewritten():
    d = tracking_domain()
    state = TubeState(p_mw=(0, 0, 0, 0), q_mvar=(0, 0, 0, 0.2), energy_mwh=1)
    admission = admit_tracking_target(
        d, state, 0, 15, (0.1, 0), d.terminal_command, "external", None, 10
    )
    assert not admission.accepted and admission.attempted_targets == ((0.1, 0),)
    assert (admission.requested_p_mw, admission.requested_q_mvar) == (0.1, 0)
    assert admission.reason


@pytest.mark.parametrize("load_after_update", [3.0, 4.0])
def test_solver_failure_renews_exact_target_only_with_a_fresh_witness(
    load_after_update,
):
    from oilfield_energy.workflows.cluster_execution.contracts import (
        ExecutionCheck,
        RegionalExecution,
    )
    from oilfield_energy.workflows.single_execution.api import run_unified_feedback
    from oilfield_energy.workflows.single_execution.contracts import (
        SingleEconomicProposal,
    )

    d = tracking_domain()
    d = d.model_copy(
        update={
            "forecasts": tuple(
                f
                if k < 15
                else f.model_copy(
                    update={
                        "loads": (
                            f.loads[0],
                            f.loads[1].model_copy(
                                update={
                                    "p_mw": I(
                                        low=load_after_update - 0.01,
                                        high=load_after_update + 0.01,
                                    )
                                }
                            ),
                        )
                    }
                )
                for k, f in enumerate(d.forecasts)
            )
        }
    )
    state = TubeState(p_mw=(0, 0, 0, 0), q_mvar=(0, 0, 0, 0.2), energy_mwh=1)
    nominal = TubeCommand(p_mw=(1, 0.4, 0, 0), q_mvar=(0, 0, 0, 0.2))

    def propose(k, actual):
        if k == 15:
            raise RuntimeError("economic solver unavailable")
        return SingleEconomicProposal(
            start_step=k,
            initial=actual,
            command=nominal,
            target_p_mw=1.6,
            target_q_mvar=0.1,
            target_origin="external",
            result=RegionalExecution(
                region="analytic",
                status="passed",
                time_minutes=(float(k),),
                p_mw=(1.6,),
                q_mvar=(0.1,),
                p_target_mw=(1.6,),
                q_target_mvar=(0.1,),
                storage_power_mw=(0,),
                checks=(
                    ExecutionCheck(
                        code="PORT_FIXTURE", label="injected proposal", status="passed"
                    ),
                ),
            ),
        )

    def observe(k):
        f = observation(d, k)
        return f.model_copy(
            update={
                "loads": tuple(
                    b.model_copy(
                        update={
                            "q_mvar": I(
                                low=(b.q_mvar.low + b.q_mvar.high) / 2,
                                high=(b.q_mvar.low + b.q_mvar.high) / 2,
                            )
                        }
                    )
                    for b in f.loads
                )
            }
        )

    u = run_unified_feedback(
        d,
        state,
        total_steps=30,
        update_steps=15,
        initial_target=(1.6, 0.1),
        observe=observe,
        propose=propose,
    )
    assert u.status == "passed" and len(u.steps) == 30, u.reason
    assert u.target_admissions[0].accepted
    renewal = u.target_admissions[1]
    assert renewal.origin == "retained"
    assert renewal.attempted_targets == ((1.6, 0.1),)
    assert all(
        (s.requested_p_mw, s.requested_q_mvar, s.deadline_step) == (1.6, 0.1, 10)
        for s in u.steps[15:]
    )
    assert "solver unavailable" in u.steps[15].proposal_reason
    if load_after_update == 3.0:
        assert renewal.accepted and u.tracking_status == "passed"
        assert all(s.disposition == "retained_feedback" for s in u.steps[15:])
        for tamper in ("target", "deadline", "disposition", "nominal", "origin"):
            raw = u.model_dump(mode="json")
            if tamper == "target":
                raw["target_admissions"][1]["requested_p_mw"] += 0.1
            elif tamper == "deadline":
                raw["target_admissions"][1]["plan"]["tracking"]["deadline_step"] += 1
            elif tamper == "nominal":
                raw["target_admissions"][1]["nominal"]["p_mw"][0] += 0.1
            elif tamper == "origin":
                raw["target_admissions"][1]["origin"] = "economic"
            else:
                raw["steps"][16]["disposition"] = "economic"
            with pytest.raises(ValueError):
                verify_unified_execution(UnifiedExecution.model_validate(raw))
    else:
        assert not renewal.accepted and renewal.reason
        assert u.tracking_status == "violated"
        assert u.target_admission_status == "rejected"
        assert u.steps[15].plan_index == u.steps[14].plan_index
        assert all(s.disposition in ("backup", "terminal") for s in u.steps[15:])
    verify_unified_execution(u)


def test_economic_tolerance_cannot_create_an_out_of_bounds_storage_command():
    d = tracking_domain()
    state = TubeState(p_mw=(0, 0, 0, 0), q_mvar=(0, 0, 0, 0.2), energy_mwh=1)
    nominal = TubeCommand(p_mw=(1, 0.4, -0.500000005, 0), q_mvar=(0, 0, 0, 0.2))
    a = admit_tracking_target(
        d, state, 0, 15, (1.6, 0.1), nominal, "economic", None, 10
    )
    assert a.accepted and a.plan is not None
    assert all(-0.5 <= c.p_mw[2] <= 0.5 for c in a.plan.commands)


def test_optimizer_failure_executes_certified_tail_and_integrates_energy_once():
    d = domain()
    c = UnifiedSingleController(d, initial())
    command = TubeCommand(p_mw=(0.6, 0.1, 0.2), q_mvar=(0, 0, 0))
    first = c.advance(
        observation(d, 0), context_id=d.context_id, target=(2.5, 0.02), proposal=command
    )
    assert first.disposition == "economic"
    assert first.after.energy_mwh == pytest.approx(1 - 0.2 / 0.95 / 60, abs=1e-15)
    certificate = c.plans[-1]
    for k in range(1, 25):
        row = c.advance(
            observation(d, k),
            context_id=d.context_id,
            target=(2.5, 0.02),
            proposal=None,
            proposal_failure="solver timeout",
        )
        assert row.command == (
            certificate.commands[k]
            if k < len(certificate.commands)
            else d.terminal_command
        )
        assert row.after.energy_mwh == first.after.energy_mwh
        assert row.deadline_step == 10
    assert c.state.p_mw == (0, 0, 0)
    evidence = UnifiedExecution(
        domain=d,
        initial=initial(),
        status="passed",
        requested_steps=25,
        steps=tuple(c.steps),
        plans=tuple(c.plans),
        terminal_proof_digest=c.terminal_proof_digest,
        tracking_status="violated",
    )
    verify_unified_execution(evidence)
    changed = c.steps[0].model_copy(update={"deadline_step": 100})
    with pytest.raises(ValueError, match="does not recompute"):
        verify_unified_execution(
            evidence.model_copy(update={"steps": (changed, *c.steps[1:])})
        )


def test_forecast_sunset_rejects_uncontinuable_pv_candidate_before_the_boundary():
    d = domain(sunset=4)
    c = UnifiedSingleController(d, initial())
    for k in range(4):
        row = c.advance(
            observation(d, k),
            context_id=d.context_id,
            target=(3, 0),
            proposal=TubeCommand(p_mw=(0, 0.1, 0), q_mvar=(0, 0, 0)),
        )
        if k == 0:
            assert row.disposition == "economic" and row.after.p_mw[1] > 0
    assert c.state.p_mw[1] == 0
    row = c.advance(
        observation(d, 4), context_id=d.context_id, target=(3, 0), proposal=None
    )
    assert row.after.p_mw[1] == 0


def test_out_of_domain_observation_or_context_never_advances_or_adopts():
    d = domain()
    c = UnifiedSingleController(d, initial())
    old_plans, old_state = tuple(c.plans), c.state
    wrong = observation(d, 0)
    wrong = wrong.model_copy(
        update={
            "loads": (
                wrong.loads[0],
                wrong.loads[1].model_copy(update={"p_mw": I(low=4, high=4)}),
            )
        }
    )
    for observed, context in ((wrong, d.context_id), (observation(d, 0), "changed")):
        with pytest.raises(ValueError):
            c.advance(
                observed, context_id=context, target=(3, 0), proposal=d.terminal_command
            )
        assert c.state == old_state and tuple(c.plans) == old_plans and not c.steps


def test_unsafe_storage_candidate_cannot_replace_available_backup():
    d = domain()
    c = UnifiedSingleController(d, initial())
    row = c.advance(
        observation(d, 0),
        context_id=d.context_id,
        target=(3, 0),
        proposal=TubeCommand(p_mw=(0, 0, 10), q_mvar=(0, 0, 0)),
    )
    assert row.disposition == "terminal" and row.after == initial()
    assert row.proposal_reason and len(c.plans) == 1

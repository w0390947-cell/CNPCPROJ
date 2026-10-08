"""Physical network coordination, independent witnesses and whole-window recovery."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.admm import CoordinationWorkspace, run_admm_coordination
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    RegionalPlanRequest,
    diagnose_regional_reference,
    realize_regional_plan,
)
from oilfield_energy.bootstrap.adapters.cluster_joint_recovery import (
    solve_joint_reference,
)
from oilfield_energy.bootstrap.adapters.cluster_recovery import (
    solve_with_reference_recovery,
)
from oilfield_energy.hierarchy_types import ADMMConfig, PhysicalCoordinationReference
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    CoordinationRecoveryPolicy,
    NetworkCandidatePolicy,
    NetworkCoordinationPolicy,
)
from oilfield_energy.workflows.cluster_execution.contracts import (
    CoordinationRecoveryReport,
    ExecutionPolicy,
)
from tests.support.network_case import bottleneck_case


def policy(**changes):
    return ExecutionPolicy(
        coordination_recovery=CoordinationRecoveryPolicy(max_recoordinations=1),
        network_candidates=NetworkCandidatePolicy(),
        network_coordination=NetworkCoordinationPolicy(joint_time_limit_seconds=15),
        **changes,
    )


def recover(case, *, config=None, calibration=None, storage=False, settings=None):
    settings = settings or policy()
    config = config or ADMMConfig(
        max_iterations=1000, network_model="branch-flow-socp-v1"
    )

    def coordinate(cal, margin, candidates=()):
        return run_admm_coordination(
            case,
            storage_enabled=storage,
            loss_calibration=cal,
            admm_config=replace(config, aggregate_pf_margin_mvar=margin),
            network_candidates={c.region: c for c in candidates}
            if candidates
            else None,
        )

    def request(window, name, ref):
        return RegionalPlanRequest(
            window,
            name,
            ref.p_references_mw[name],
            ref.q_references_mvar[name],
            storage,
            10,
            settings,
        )

    return solve_with_reference_recovery(
        case=case,
        policy=settings,
        calibration=calibration,
        coordinate=coordinate,
        coordinate_candidates=coordinate,
        storage_enabled=storage,
        aggregate_pf_target=config.aggregate_pf_target,
        solve_regions=lambda w, r: {
            m.name: realize_regional_plan(request(w, m.name, r)) for m in w.microgrids
        },
        diagnose_regions=lambda w, r, n: tuple(
            diagnose_regional_reference((request(w, m.name, r), n))
            for m in w.microgrids
        ),
    )


def test_original_line_bottleneck_is_feasible_in_first_network_coordination():
    case = bottleneck_case()
    result = recover(case)
    assert result.report.accepted, result.report
    assert len(result.report.rounds) == 1
    row = result.report.rounds[0]
    assert row.reference_method == "network-socp-admm" and row.admm_iterations > 0
    assert row.candidate_selection is None and row.joint_network is None
    for name, (evidence, plan) in result.payload.plans.items():
        physical = plan.microgrids[name]
        assert evidence.executable_plan and evidence.p_mw[0] > 4.0
        assert max(physical["pv_used_mw"]) <= 1.0 + 1e-6
        np.testing.assert_allclose(
            evidence.p_mw, 5 - physical["pv_used_mw"] + physical["loss_mw"], atol=1e-6
        )


def test_upper_voltage_bottleneck_informs_normal_network_reference():
    result = recover(bottleneck_case(rating=5, voltage_max=1.001))
    assert result.report.accepted, result.report
    assert result.report.rounds[0].reference_method == "network-socp-admm"
    assert all(v[0] > 4 for v in result.report.rounds[0].p_target_mw.values())
    for row, plan in result.payload.plans.values():
        assert row.executable_plan
        assert np.max(plan.microgrids[row.region]["voltage_pu"]) <= 1.001 + 1e-6


@pytest.mark.parametrize("capacitive", [True, False])
def test_network_reference_computes_voltage_dependent_shunt_without_calibration(
    capacitive,
):
    from oilfield_energy.network_model import ShuntKind
    from tests.integration.dispatch.test_fixed_shunt_coordination import fixed_case

    case = fixed_case(ShuntKind.CAPACITOR if capacitive else ShuntKind.REACTOR)
    result = recover(case)
    assert result.report.accepted, result.report
    assert len(result.report.rounds) == 1
    for name, (row, plan) in result.payload.plans.items():
        data = plan.microgrids[name]
        mg = next(m for m in case.microgrids if m.name == name)
        expected_q = (
            sum(mg.load_q_mvar.values())
            + data["reactive_loss_mvar"]
            - sum(data["fixed_shunt_q_mvar_by_bus"].values())
        )
        np.testing.assert_allclose(row.q_mvar, expected_q, atol=1e-6)
        np.testing.assert_allclose(
            result.report.rounds[0].q_target_mvar[name], expected_q, atol=1e-5
        )


def test_bus_and_region_order_do_not_change_network_constraints():
    case = bottleneck_case()
    permuted = replace(
        case,
        microgrids=[
            replace(
                m,
                buses=list(reversed(m.buses)),
                network_model_v2=replace(
                    m.network_model_v2, buses=tuple(reversed(m.network_model_v2.buses))
                ),
            )
            for m in reversed(case.microgrids)
        ],
    )
    base, changed = recover(case), recover(permuted)
    assert base.report.accepted and changed.report.accepted
    for name in base.payload.reference.p_references_mw:
        np.testing.assert_allclose(
            base.payload.reference.p_references_mw[name],
            changed.payload.reference.p_references_mw[name],
            atol=1e-5,
        )


def test_joint_search_finds_new_combination_when_certified_candidate_pool_cannot():
    narrow, wide = bottleneck_case(), bottleneck_case(rating=5)
    case = replace(
        narrow,
        microgrids=[narrow.microgrids[0], *wide.microgrids[1:]],
        cluster_import_limit_mw=7,
    )
    witness = solve_case_ac_consistent(
        case, storage_enabled=False, time_limit_seconds=10
    )
    assert witness.passed
    # Explicit stale loss estimates make B/C ask for about 2 MW. Local soft
    # diagnostics then total > 8 MW; a physically possible joint plan uses < 7.
    calibration = {
        n: CoordinationNetworkCalibration((0.026 if n == "A" else 1.0,), (0.03,))
        for n in ("A", "B", "C")
    }
    config = ADMMConfig(max_iterations=1000)
    result = recover(case, calibration=calibration, config=config)
    assert result.report.accepted, result.report
    assert result.report.rounds[0].candidate_selection.status == "no_joint_candidate"
    candidates = result.report.rounds[0].feedback
    assert sum(c.evidence.p_mw[0] for c in candidates) > 7
    final = result.report.rounds[-1]
    assert final.reference_method == "joint-network" and final.admm_iterations == 0
    assert final.joint_network.status == "certified"
    assert isinstance(result.payload.reference, PhysicalCoordinationReference)
    assert final.cluster_import.status == final.cluster_power_factor.status == "passed"
    assert sum(r.p_mw[0] for r in final.regions) <= 7 + 1e-6
    assert all(r.executable_plan for r in final.regions)
    assert (
        CoordinationRecoveryReport.model_validate_json(result.report.model_dump_json())
        == result.report
    )
    assert all(np.array_equal(m.pv_available_mw["pv"], [4.0]) for m in case.microgrids)


def test_joint_solver_keeps_storage_energy_and_integer_modes_for_whole_window():
    case = replace(bottleneck_case(steps=2), price_cny_per_mwh=np.array([800.0, 200.0]))
    reference = run_admm_coordination(case, admm_config=ADMMConfig(max_iterations=1000))
    target, evidence = solve_joint_reference(
        case, reference, policy(), True, 0.95, (0.0, 0.0)
    )
    assert target is not None and evidence.status == "certified", evidence
    for m in case.microgrids:
        row, plan = realize_regional_plan(
            RegionalPlanRequest(
                case,
                m.name,
                target.p_references_mw[m.name],
                target.q_references_mvar[m.name],
                True,
                10,
                policy(),
            )
        )
        assert row.executable_plan
        data = plan.microgrids[m.name]
        assert np.max(data["storage_charge_mw"] * data["storage_discharge_mw"]) < 1e-7
        np.testing.assert_allclose(
            np.diff(row.storage_energy_mwh),
            case.assumptions.dt_hours
            * (
                m.storage.eta_charge * data["storage_charge_mw"]
                - data["storage_discharge_mw"] / m.storage.eta_discharge
            ),
            atol=1e-6,
        )


def test_network_cache_updates_loads_and_does_not_retain_line_limits():
    cache = CoordinationWorkspace()
    cfg = ADMMConfig(max_iterations=1000, network_model="branch-flow-socp-v1")
    first = bottleneck_case()
    for case in (
        first,
        bottleneck_case(rating=5),
        replace(
            first,
            microgrids=[
                replace(m, load_p_mw={b: v * 0.8 for b, v in m.load_p_mw.items()})
                for m in first.microgrids
            ],
        ),
    ):
        reused = run_admm_coordination(
            case, storage_enabled=False, admm_config=cfg, workspace=cache
        )
        fresh = run_admm_coordination(case, storage_enabled=False, admm_config=cfg)
        assert reused.converged and fresh.converged
        for name in fresh.p_references_mw:
            np.testing.assert_allclose(
                reused.p_references_mw[name], fresh.p_references_mw[name], atol=1e-5
            )
    assert np.array_equal(first.microgrids[0].load_p_mw["load"], [5.0])


@pytest.mark.parametrize("target", [0.95, 0.9999])
def test_joint_reference_obeys_explicit_aggregate_pf_and_margin(target):
    case = bottleneck_case()
    old = run_admm_coordination(
        case, storage_enabled=False, admm_config=ADMMConfig(max_iterations=1000)
    )
    ref, evidence = solve_joint_reference(case, old, policy(), False, target, (0.02,))
    assert evidence.status == "certified", evidence
    p = sum(v[0] for v in ref.p_references_mw.values())
    q = sum(v[0] for v in ref.q_references_mvar.values())
    assert abs(q) + 0.02 <= np.tan(np.arccos(target)) * p + 1e-6


def test_joint_model_infeasibility_is_not_labeled_original_ac_infeasibility():
    case = bottleneck_case()
    old = run_admm_coordination(
        case, storage_enabled=False, admm_config=ADMMConfig(max_iterations=1000)
    )
    target, evidence = solve_joint_reference(
        replace(case, cluster_import_limit_mw=2.0), old, policy(), False, 0.95, (0.0,)
    )
    assert target is None and evidence.status == "model_infeasible"

"""Real single-service planning, AC checks, device execution and feedback."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.cluster_rolling import window_case
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation
from oilfield_energy.workflows.single_execution.api import verify_unified_execution


def small_case(flat=False):
    case = build_synthetic_case(96)
    energies = {m.name: m.storage.e_initial_mwh for m in case.microgrids}
    case = window_case(case, 0, 2, energies, energies)
    if flat:
        case = replace(
            case,
            microgrids=[
                replace(
                    m,
                    **{
                        key: {b: np.full(2, v[0]) for b, v in getattr(m, key).items()}
                        for key in (
                            "load_p_mw",
                            "load_q_mvar",
                            "wind_available_mw",
                            "pv_available_mw",
                        )
                    },
                )
                for m in case.microgrids
            ],
        )
    return case


@pytest.mark.parametrize(
    "region,storage", [("SC", True), ("YA_B", False), ("YA_C", True)]
)
def test_public_single_path_uses_joint_planning_and_actual_feedback(
    monkeypatch, region, storage
):
    import oilfield_energy.admm as admm
    import oilfield_energy.bootstrap.adapters.single_unified as adapter
    import oilfield_energy.service as service

    seen = []
    original = adapter.realize_regional_plan

    def solve(request):
        seen.append(request)
        return original(request)

    def no_admm(*args, **kwargs):
        pytest.fail("autonomous single execution must not call ADMM")

    monkeypatch.setattr(adapter, "realize_regional_plan", solve)
    monkeypatch.setattr(admm, "run_admm_coordination", no_admm)
    monkeypatch.setattr(service, "run_admm_coordination", no_admm)
    result = run_simulation(
        SimulationRequest(
            steps=2,
            region=region,
            storage_enabled=storage,
            solver={"quality_policy": "quality-first-v1"},
        ),
        input_case=small_case(flat=not storage),
    )
    e = result.single_execution
    assert e is not None and e.status == "passed", e
    assert result.executive_summary.overall_passed == all(
        c.passed for c in result.validation_items
    )
    assert result.cluster_execution is None and not result.admm_history
    u = e.unified
    assert u is not None and u.status == "passed" and u.tracking_status == "passed"
    assert len(seen) == len(u.economic_proposals) == 2
    assert e.completed_minutes == e.total_minutes == 30
    assert len(u.steps) == 30
    for admission in u.target_admissions:
        assert admission.selected_candidate is not None
        selected = admission.candidate_economic_costs_cny[admission.selected_candidate]
        assert selected == min(
            c for c in admission.candidate_economic_costs_cny if c is not None
        )
    assert not e.startup_transition_assessed
    assert u.economic_proposals[1].initial == u.steps[14].after
    for request, w in zip(seen, u.economic_proposals, strict=True):
        assert request.p_target_mw is request.q_target_mvar is None
        assert [m.name for m in request.case.microgrids] == [region]
        assert request.case.microgrids[0].storage.e_initial_mwh == w.initial.energy_mwh
        assert request.case.joint_pq_plans[region].initial == w.initial
        assert w.pq_witness is not None
        assert request.case.joint_pq_plans[region] == w.pq_witness.inputs
        assert not request.case.reactive_plans
    if not storage:
        index = next(
            i for i, r in enumerate(u.domain.devices.resources) if r.kind == "storage"
        )
        np.testing.assert_allclose(
            [s.after.p_mw[index] for s in u.steps], 0.0, atol=1e-9
        )
        np.testing.assert_allclose(
            [s.after.energy_mwh for s in u.steps],
            e.initial_state.storage_energy_mwh,
            atol=1e-9,
        )
    restored = SimulationResult.model_validate_json(result.model_dump_json())
    assert restored.single_execution == e


def test_failed_planning_retains_joint_tail_when_target_renewal_conflicts_with_task(
    monkeypatch,
):
    import oilfield_energy.bootstrap.adapters.single_unified as adapter

    original = adapter.realize_regional_plan
    calls = 0

    def solve(request):
        nonlocal calls
        calls += 1
        if calls >= 2:
            mg = request.case.microgrids[0]
            # Excessive demand makes the actual planning model infeasible.
            impossible = replace(
                request.case,
                microgrids=[
                    replace(
                        mg,
                        load_p_mw={
                            b: np.full_like(v, 1e4) for b, v in mg.load_p_mw.items()
                        },
                    )
                ],
            )
            request = replace(request, case=impossible)
        return original(request)

    monkeypatch.setattr(adapter, "realize_regional_plan", solve)
    r = run_simulation(SimulationRequest(steps=2), input_case=small_case())
    e = r.single_execution
    assert not r.executive_summary.overall_passed
    assert e.status == "violated" and e.completed_minutes == 30
    u = e.unified
    assert u.status == "passed" and u.tracking_status == "violated"
    assert u.hard_task.guarantee == "maintained" and u.hard_task.outcome == "met"
    assert u.economic_proposals[1].initial == u.steps[14].after
    assert u.economic_proposals[1].command is None
    assert u.economic_proposals[1].result.status != "passed"
    assert u.target_admissions[1].origin == "retained"
    assert not u.target_admissions[1].accepted
    assert "energy reachability" in u.target_admissions[1].reason
    assert all(s.disposition in ("backup", "terminal") for s in u.steps[15:])
    assert all(s.deadline_step == u.steps[14].deadline_step for s in u.steps[15:])
    assert all(
        (s.requested_p_mw, s.requested_q_mvar)
        == (u.steps[14].requested_p_mw, u.steps[14].requested_q_mvar)
        for s in u.steps[15:]
    )
    assert len(u.steps) == 30
    verify_unified_execution(u)


def test_execution_tracking_failure_cannot_be_hidden_by_safe_plans(monkeypatch):
    import oilfield_energy.bootstrap.adapters.single_unified as adapter

    original = adapter.SingleNumericalPorts.propose

    def unreachable(self, step, state):
        proposal = original(self, step, state)
        # Exercise external goal rejection, rather than corrupting the bound
        # internal economic point (which must fail before target admission).
        return proposal.model_copy(
            update={"target_p_mw": 100.0, "target_origin": "external"}
        )

    monkeypatch.setattr(adapter.SingleNumericalPorts, "propose", unreachable)
    r = run_simulation(
        SimulationRequest(steps=2, region="YA_B", storage_enabled=False),
        input_case=small_case(),
    )
    e = r.single_execution
    assert e.unified.status == "passed"
    assert e.completed_minutes == 30
    assert e.status == "violated" and not r.executive_summary.overall_passed
    assert e.unified.tracking_status == "violated"
    assert any(
        c.code == "SINGLE_CYCLE_TRACKING" and not c.passed for c in r.validation_items
    )
    assert e.unified.target_admission_status == "rejected"
    assert len(e.unified.target_admissions) == 2
    assert all(not a.accepted for a in e.unified.target_admissions)
    assert any(
        c.code == "SINGLE_TARGET_ACCEPTANCE" and not c.passed
        for c in r.validation_items
    )


def test_historical_result_does_not_acquire_execution_evidence():
    r = run_simulation(SimulationRequest(steps=2), input_case=small_case())
    old = r.model_dump(mode="json")
    del old["single_execution"]
    with pytest.raises(ValueError, match="current unified execution evidence"):
        SimulationResult.model_validate(old)


def test_legacy_solver_cannot_bypass_the_unified_execution():
    with pytest.raises(ValueError, match="unified MISOCP"):
        SimulationRequest(steps=2, solver={"formulation": "legacy_milp"})


def test_cli_uses_the_same_independent_workflow(monkeypatch, tmp_path):
    import json

    import oilfield_energy.cli as cli

    run = cli.run_single_execution
    monkeypatch.setattr(cli, "build_synthetic_case", lambda steps: small_case())
    monkeypatch.setattr(cli, "plot_microgrid", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "plot_cluster", lambda *args, **kwargs: None)

    def explicit_case(*args, **kwargs):
        # This fixture is a 30-minute captured case, not the packaged full day.
        kwargs["packaged_inputs"] = False
        return run(*args, **kwargs)

    monkeypatch.setattr(cli, "run_single_execution", explicit_case)
    cli.run(2, tmp_path, 30)
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    evidence = summary["single_microgrid"]["unified_execution"]
    assert evidence["completed_minutes"] == 30
    assert len(evidence["unified"]["economic_proposals"]) == 2
    assert summary["single_microgrid"]["unified_execution_passed"]
    assert summary["multi_microgrid"]["execution_scope"] == "reference_only"

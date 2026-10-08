"""The public service uses the proof supervisor and a real MISOCP proposal source."""

import numpy as np
import pytest

from oilfield_energy.data import (
    Line,
    MicrogridData,
    ModelAssumptions,
    ProjectCase,
    Storage,
)
from oilfield_energy.modules.control.contracts import TubeState
from oilfield_energy.modules.power_flow.contracts import PowerInterval as I
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation
from oilfield_energy.workflows.single_execution.api import verify_unified_execution
from oilfield_energy.workflows.single_execution.contracts import (
    BoundedBusLoad,
    FirmGeneration,
    SingleBoundaryRequest,
)


def study():
    mg = MicrogridData(
        name="SC",
        buses=["pcc", "load"],
        lines=[Line("l", "pcc", "load", 0.01, 0.02, 10)],
        pcc_bus="pcc",
        base_mva=10,
        maximum_load_mw=3,
        p_grid_min_mw=0.1,
        p_grid_max_mw=5,
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        load_p_mw={"pcc": np.zeros(2), "load": np.full(2, 3.0)},
        load_q_mvar={"pcc": np.zeros(2), "load": np.full(2, 0.3)},
        wind_available_mw={"load": np.full(2, 1.5)},
        wind_capacity_mw={"load": 2.0},
        wind_capacity_mva={"load": 2.2},
        pv_available_mw={},
        pv_capacity_mw={},
        pv_capacity_mva={},
        storage=Storage("load", 0.5, 2, 0.2, 1, 0.95, 0.95, 1),
        svg_bus="load",
        svg_q_min_mvar=-1,
        svg_q_max_mvar=1,
        wind_q_abs_over_p_max={"load": 0.3},
    )
    case = ProjectCase(
        time_hours=np.array([0.0, 0.25]),
        price_cny_per_mwh=np.full(2, 400.0),
        microgrids=[mg],
        assumptions=ModelAssumptions(),
        cluster_import_limit_mw=5,
    )
    settings = SingleBoundaryRequest(
        source="explicit bounded two-bus algorithm example",
        loads=(
            BoundedBusLoad(bus="pcc", p_mw=I(low=0, high=0), q_mvar=I(low=0, high=0)),
            BoundedBusLoad(
                bus="load",
                p_mw=I(low=2.999, high=3.001),
                q_mvar=I(low=0.299, high=0.301),
            ),
        ),
        generation=(
            FirmGeneration(resource_id="SC:wind:load", minimum_available_mw=1.5),
        ),
        target_p_mw=2.0040595683,
        target_q_mvar=0.2081191366,
        initial_resource_ids=("SC:wind:load", "SC:storage:load", "SC:svg:load"),
        initial_state=TubeState(p_mw=(0.8, 0, 0), q_mvar=(0, 0, 0), energy_mwh=1),
    )
    return case, settings


@pytest.fixture(scope="module")
def result():
    case, settings = study()
    return run_simulation(
        SimulationRequest(steps=2, single_boundary=settings), input_case=case
    )


def test_declared_boundary_uses_the_only_formal_executor(result):
    e = result.single_execution
    u = e.unified
    assert e.version == "single-execution-v9"
    assert u.status == "passed" and u.tracking_status == "passed"
    assert e.status == "passed" and e.completed_minutes == 30
    assert e.initialization == "declared_actual_state"
    assert u.domain.bound_source == study()[1].source
    assert u.economic_proposals[1].initial == u.steps[14].after
    assert (
        SimulationResult.model_validate_json(result.model_dump_json()).single_execution
        == e
    )
    verify_unified_execution(u)


def test_real_entry_recertifies_the_same_target_when_the_next_optimizer_fails(
    monkeypatch,
):
    import oilfield_energy.bootstrap.adapters.single_unified as adapter

    case, settings = study()
    original = adapter.SingleNumericalPorts.propose

    def proposal(self, step, state):
        if step == 15:
            raise RuntimeError("solver time limit without a new incumbent")
        return original(self, step, state)

    monkeypatch.setattr(adapter.SingleNumericalPorts, "propose", proposal)
    r = run_simulation(
        SimulationRequest(steps=2, single_boundary=settings), input_case=case
    )
    u = r.single_execution.unified
    assert u.status == "passed" and len(u.steps) == 30
    assert "time limit" in u.steps[15].proposal_reason
    assert u.tracking_status == "passed"
    assert u.steps[15].plan_index == u.steps[14].plan_index + 1
    assert u.target_admissions[1].origin == "retained"
    assert all(s.disposition == "retained_feedback" for s in u.steps[15:])
    assert all(s.deadline_step == u.steps[14].deadline_step for s in u.steps[15:])
    assert all(
        (s.requested_p_mw, s.requested_q_mvar)
        == (u.steps[14].requested_p_mw, u.steps[14].requested_q_mvar)
        for s in u.steps[15:]
    )
    verify_unified_execution(u)


def test_missing_or_inapplicable_bounds_cannot_be_silently_enabled():
    _, settings = study()
    with pytest.raises(ValueError, match="single boundary"):
        SimulationRequest(
            scenario_type="cluster_coordination", single_boundary=settings
        )
    with pytest.raises(ValueError):
        SingleBoundaryRequest(source="missing bounds")


@pytest.mark.parametrize(
    "tamper",
    [
        "state",
        "network_bound",
        "deadline",
        "command",
        "policy",
        "admission",
        "regulator",
        "economic_cost",
        "participation",
        "allocation_score",
        "search_outcome",
    ],
)
def test_modified_serialized_evidence_cannot_survive_proof_recomputation(
    result, tamper
):
    from oilfield_energy.workflows.single_execution.contracts import UnifiedExecution

    raw = result.single_execution.unified.model_dump(mode="json")
    if tamper == "state":
        raw["steps"][-1]["after"]["energy_mwh"] += 0.1
    elif tamper == "network_bound":
        raw["steps"][-1]["pcc_p_mw"]["high"] -= 0.001
    elif tamper == "deadline":
        raw["steps"][-1]["deadline_step"] += 10
    elif tamper == "admission":
        raw["target_admissions"][0]["requested_p_mw"] += 0.1
    elif tamper == "economic_cost":
        admission = raw["target_admissions"][0]
        admission["candidate_economic_costs_cny"][admission["selected_candidate"]] += 1
    elif tamper == "participation":
        plan = raw["target_admissions"][0]["plan"]
        plan["tracking"]["policies"][-1]["p_participation"][0] += 1
    elif tamper == "allocation_score":
        t = raw["target_admissions"][0]["plan"]["tracking"]
        selected = t["construction"]
        t["allocation_trials"][selected]["intervention"] += 1
    elif tamper == "search_outcome":
        a = raw["target_admissions"][0]
        a["candidate_outcomes"][a["selected_candidate"]] = "construction_unproven"
    elif tamper == "policy":
        plan = raw["target_admissions"][0]["plan"]
        plan["tracking"]["policies"][-1]["net_p_mw"] += 0.1
    elif tamper == "regulator":
        row = raw["steps"][10]
        storage = next(
            i
            for i, r in enumerate(raw["domain"]["devices"]["resources"])
            if r["kind"] == "storage"
        )
        row["command"]["p_mw"][storage] += 0.01
    else:
        raw["steps"][-1]["command"]["p_mw"][0] += 0.01
    with pytest.raises(ValueError):
        verify_unified_execution(UnifiedExecution.model_validate(raw))


def test_initial_joint_task_unproven_still_runs_public_safe_simulation():
    case, boundary = study()
    boundary = boundary.model_copy(
        update={
            "initial_state": boundary.initial_state.model_copy(
                update={"energy_mwh": 1.8}
            )
        }
    )
    result = run_simulation(
        SimulationRequest(steps=2, single_boundary=boundary), input_case=case
    )
    execution = result.single_execution
    assert execution.completed_minutes == 30
    assert execution.unified.status == "passed"
    assert execution.unified.hard_task.guarantee == "unproven"
    assert execution.unified.hard_task.outcome == "missed"
    assert execution.status != "passed"
    item = next(c for c in result.validation_items if c.code == "SINGLE_HARD_TASK")
    assert not item.passed and item.assessment_status == "violated"
    verify_unified_execution(execution.unified)

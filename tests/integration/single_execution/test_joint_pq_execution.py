"""Real MISOCP and public execution keep actual P/Q and explicit witnesses."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.single_model import (
    assemble_single_physical_model,
)
from oilfield_energy.bootstrap.adapters.single_planning import (
    prepare_single_case,
    single_execution_policy,
)
from oilfield_energy.misocp_model import solve_case_misocp
from oilfield_energy.modules.control.contracts import DeviceCheckpoint, TubeState
from oilfield_energy.modules.dispatch.api import pq_witness_violation
from oilfield_energy.modules.dispatch.contracts import (
    PQPlanningInput,
    PQPlanningWitness,
)
from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.workflows.single_execution.api import verify_unified_execution
from oilfield_energy.workflows.single_execution.contracts import UnifiedExecution
from tests.integration.single_execution.test_declared_boundary import study


def test_misocp_joint_witness_recovers_a_transition_rejected_by_fixed_p_rows():
    case, _ = study()
    mg = replace(
        case.microgrids[0],
        wind_available_mw={"load": np.full(2, 1.811699)},
        load_q_mvar={"pcc": np.zeros(2), "load": np.full(2, 0.6)},
    )
    case = replace(case, microgrids=[mg])
    policy = single_execution_policy()
    _, model = assemble_single_physical_model(mg, case, policy, storage_enabled=True)
    state = TubeState(p_mw=(2, 0, 0), q_mvar=(0.552596, 0, 0), energy_mwh=1)
    ids = tuple(r.resource_id for r in model.resources)
    checkpoint = DeviceCheckpoint(
        minute=0,
        storage_energy_mwh=1,
        storage_power_mw=0,
        active_power_mw=dict(zip(ids, state.p_mw)),
        reactive_power_mvar=dict(zip(ids, state.q_mvar)),
    )
    old = prepare_single_case(
        case, "SC", storage_enabled=True, policy=policy, checkpoint=checkpoint
    )
    rejected = solve_case_misocp(
        old, ["SC"], cluster_coordination=False, time_limit_seconds=10
    )
    assert not rejected.success
    ceilings = (tuple(r.p.high for r in model.resources),) + tuple(
        (1.811699, 0.5, 0) for _ in range(30)
    )
    inputs = PQPlanningInput(
        devices=model,
        initial=state,
        planning_steps=2,
        planning_minutes=15,
        transition_minutes=5,
        p_ceiling_mw=ceilings,
        polygon_sides=16,
        reserve_fraction=0.1,
    )
    new = prepare_single_case(
        case,
        "SC",
        storage_enabled=True,
        policy=policy,
        checkpoint=checkpoint,
        joint_pq=inputs,
    )
    solved = solve_case_misocp(
        new, ["SC"], cluster_coordination=False, time_limit_seconds=15
    )
    assert solved.success, solved.message
    witness = PQPlanningWitness.model_validate(
        solved.microgrids["SC"]["joint_pq_witness"]
    )
    assert witness.inputs.initial == state and pq_witness_violation(witness) < 1e-6
    assert not new.reactive_plans
    assert case.microgrids[0].wind_available_mw["load"][0] == 1.811699
    with pytest.raises(ValueError, match="exact window"):
        replace(new, time_hours=np.asarray([0, 1 / 6]))
    # A true first-minute shortage remains infeasible under this device's ramp.
    assert 2 - model.resources[0].ramp_p_mw > 0.5
    tight = inputs.model_copy(
        update={"p_ceiling_mw": (ceilings[0],) + ((0.5, 0.5, 0),) * 30}
    )
    impossible = prepare_single_case(
        case,
        "SC",
        storage_enabled=True,
        policy=policy,
        checkpoint=checkpoint,
        joint_pq=tight,
    )
    assert not solve_case_misocp(
        impossible,
        ["SC"],
        cluster_coordination=False,
        time_limit_seconds=10,
    ).success


def test_misocp_positive_pv_can_land_on_a_future_zero_ceiling():
    from oilfield_energy.modules.control.api import execute_tube_command

    case, _ = study()
    mg = replace(
        case.microgrids[0],
        pv_available_mw={"load": np.array([0.6, 0.0])},
        pv_capacity_mw={"load": 0.6},
        pv_capacity_mva={"load": 0.7},
    )
    case = replace(case, microgrids=[mg])
    policy = single_execution_policy()
    _, model = assemble_single_physical_model(mg, case, policy, storage_enabled=True)
    ids = tuple(r.resource_id for r in model.resources)
    pv = next(i for i, r in enumerate(model.resources) if r.kind == "pv")
    state = TubeState(
        p_mw=tuple(0.5 if i == pv else 0 for i in range(len(ids))),
        q_mvar=(0,) * len(ids),
        energy_mwh=1,
    )
    caps = tuple(r.p.high for r in model.resources)
    zero_caps = tuple(0 if i == pv else p for i, p in enumerate(caps))
    inputs = PQPlanningInput(
        devices=model,
        initial=state,
        planning_steps=2,
        planning_minutes=15,
        transition_minutes=5,
        p_ceiling_mw=(caps,) * 16 + (zero_caps,) * 15,
        polygon_sides=16,
        reserve_fraction=0.1,
    )
    checkpoint = DeviceCheckpoint(
        minute=0,
        storage_energy_mwh=1,
        storage_power_mw=0,
        active_power_mw=dict(zip(ids, state.p_mw)),
        reactive_power_mvar=dict(zip(ids, state.q_mvar)),
    )
    candidate = prepare_single_case(
        case,
        "SC",
        storage_enabled=True,
        policy=policy,
        checkpoint=checkpoint,
        joint_pq=inputs,
    )
    solved = solve_case_misocp(
        candidate, ["SC"], cluster_coordination=False, time_limit_seconds=15
    )
    assert solved.success, solved.message
    w = PQPlanningWitness.model_validate(solved.microgrids["SC"]["joint_pq_witness"])
    assert pq_witness_violation(w) < 1e-6
    actual = state
    for t, commands in enumerate(w.commands):
        for u in (*commands, *(w.endpoints[t],) * 10):
            actual = execute_tube_command(model, actual, u)
        if t == 1:
            assert actual.p_mw[pv] == 0
    assert w.endpoints[-1].p_mw[pv] == 0


@pytest.fixture(scope="module")
def execution():
    case, boundary = study()
    result = run_simulation(
        SimulationRequest(steps=2, single_boundary=boundary), input_case=case
    )
    return result.single_execution.unified


def test_real_entry_records_joint_planning_and_completed_state(execution):
    u = execution
    assert len(u.steps) == 30 and u.status == u.tracking_status == "passed"
    for p in u.economic_proposals:
        assert p.pq_witness is not None
        assert p.pq_witness.inputs.initial == u.steps[p.start_step].before
        assert any(
            c.code == "JOINT_PQ_PLAN" and c.status == "passed" for c in p.result.checks
        )
    verify_unified_execution(u)


def test_invalid_nominal_proposal_does_not_contaminate_retained_safe_evidence(
    monkeypatch,
):
    import oilfield_energy.bootstrap.adapters.single_unified as adapter

    original = adapter.SingleNumericalPorts.propose

    def propose(self, step, state):
        p = original(self, step, state)
        if step == 15:
            w = p.pq_witness
            assert w is not None
            rows = list(w.inputs.p_ceiling_mw)
            rows[1] = (rows[1][0] - 0.1, *rows[1][1:])
            p = p.model_copy(
                update={
                    "pq_witness": w.model_copy(
                        update={
                            "inputs": w.inputs.model_copy(
                                update={"p_ceiling_mw": tuple(rows)}
                            )
                        }
                    )
                }
            )
        return p

    monkeypatch.setattr(adapter.SingleNumericalPorts, "propose", propose)
    case, boundary = study()
    result = run_simulation(
        SimulationRequest(steps=2, single_boundary=boundary), input_case=case
    )
    u = result.single_execution.unified
    assert u.status == u.tracking_status == "passed" and len(u.steps) == 30
    assert "nominal witness" in u.steps[15].proposal_reason
    assert [p.start_step for p in u.economic_proposals] == [0]
    assert all(s.disposition == "retained_feedback" for s in u.steps[15:])
    assert u.steps[15].deadline_step == u.steps[14].deadline_step
    verify_unified_execution(u)


@pytest.mark.parametrize(
    "tamper",
    [
        "initial_p",
        "command_q",
        "future_cap",
        "missing_witness",
        "time_grid",
        "missing_resource",
        "extra_resource",
        "storage_power",
        "proposal_command",
        "internal_request",
    ],
)
def test_nominal_witness_tampering_cannot_survive_recomputation(execution, tamper):
    raw = execution.model_dump(mode="json")
    proposal = raw["economic_proposals"][0]
    w = proposal["pq_witness"]
    if tamper == "initial_p":
        w["inputs"]["initial"]["p_mw"][0] += 0.1
    elif tamper == "command_q":
        w["commands"][0][0]["q_mvar"][0] += 0.1
    elif tamper == "future_cap":
        w["inputs"]["p_ceiling_mw"][1][0] -= 0.1
    elif tamper == "time_grid":
        proposal["result"]["time_minutes"][1] += 1
    elif tamper == "missing_resource":
        del proposal["result"]["resource_p_mw"][
            w["inputs"]["devices"]["resources"][0]["resource_id"]
        ]
    elif tamper == "extra_resource":
        proposal["result"]["resource_q_mvar"]["unknown"] = [0] * len(w["endpoints"])
    elif tamper == "storage_power":
        proposal["result"]["storage_power_mw"][0] += 0.1
    elif tamper == "proposal_command":
        proposal["command"]["p_mw"][0] += 0.1
    elif tamper == "internal_request":
        proposal["target_origin"] = "economic"
        proposal["target_p_mw"] += 0.1
    else:
        proposal["pq_witness"] = None
    with pytest.raises(ValueError):
        verify_unified_execution(UnifiedExecution.model_validate(raw))

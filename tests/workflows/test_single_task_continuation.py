"""Analytic task completion and failures, independent of the oilfield day curve."""

from fractions import Fraction as F

import pytest

from oilfield_energy.modules.control.api import (
    certify_energy_return,
    energy_return_command,
    execute_tube_command,
    point_box,
    tube_contains,
)
from oilfield_energy.modules.control.contracts import EnergyReturnPolicy, TubeState
from oilfield_energy.modules.power_flow.contracts import PowerInterval as I
from oilfield_energy.workflows.single_execution.api import (
    run_unified_feedback,
    verify_unified_execution,
)
from oilfield_energy.workflows.single_execution.contracts import TerminalEnergyTask
from tests.workflows.test_unified_single import domain, observation


def task_domain(deadline=30):
    d = domain()
    return d.model_copy(
        update={
            "energy_task": TerminalEnergyTask(
                task_id="analytic-energy",
                deadline_step=deadline,
                energy_mwh=I(low=0.99, high=1.01),
                source="analytic test specification",
            )
        }
    )


def unavailable(step, state):
    raise RuntimeError("injected solver timeout")


@pytest.mark.parametrize("energy", [0.94, 1.0, 1.06])
def test_retained_task_completes_despite_every_economic_solve_failing(energy):
    d = task_domain()
    initial = TubeState(p_mw=(0, 0, 0), q_mvar=(0, 0, 0), energy_mwh=energy)
    result = run_unified_feedback(
        d,
        initial,
        total_steps=30,
        update_steps=5,
        initial_target=(3, 0),
        observe=lambda k: observation(d, k),
        propose=unavailable,
    )
    assert result.status == "passed"
    assert result.hard_task.guarantee == "maintained"
    assert result.hard_task.outcome == "met"
    assert 0.99 <= result.steps[-1].after.energy_mwh <= 1.01
    # Independent energy conservation, including charge/discharge efficiencies.
    expected = F(energy)
    for row in result.steps:
        p = F(row.command.p_mw[2])
        expected += (-p / F(0.95) if p >= 0 else -p * F(0.95)) / 60
    assert abs(float(expected) - result.steps[-1].after.energy_mwh) < 1e-13
    verify_unified_execution(result)


def test_initial_joint_failure_continues_safety_and_reports_missed_task():
    d = task_domain(deadline=1)
    initial = TubeState(p_mw=(0, 0, 0), q_mvar=(0, 0, 0), energy_mwh=1.4)
    result = run_unified_feedback(
        d,
        initial,
        total_steps=5,
        update_steps=5,
        initial_target=(3, 0),
        observe=lambda k: observation(d, k),
        propose=unavailable,
    )
    assert result.status == "passed" and len(result.steps) == 5
    assert result.hard_task.guarantee == "unproven"
    assert result.hard_task.outcome == "missed"
    assert not result.hard_task.initial_certified
    verify_unified_execution(result)


@pytest.mark.parametrize(
    "eta_charge,eta_discharge,step_minutes",
    [(0.95, 0.95, 1), (0.8, 0.9, 0.5), (1, 1, 2)],
)
def test_energy_feedback_encloses_interior_values_and_saturation(
    eta_charge, eta_discharge, step_minutes
):
    d = task_domain()
    d = d.model_copy(
        update={
            "devices": d.devices.model_copy(
                update={
                    "eta_charge": eta_charge,
                    "eta_discharge": eta_discharge,
                    "step_minutes": step_minutes,
                }
            )
        }
    )
    initial = TubeState(p_mw=(0, 0, 0), q_mvar=(0, 0, 0), energy_mwh=1)
    source = point_box(initial).model_copy(
        update={"energy_mwh": I(low=0.98, high=1.02)}
    )
    policy = EnergyReturnPolicy(
        base=d.terminal_command,
        target_mwh=1,
        charge_limit_mw=0.2,
        discharge_limit_mw=0.2,
    )
    proof = certify_energy_return(d.devices, source, policy)
    for j in range(101):
        state = initial.model_copy(update={"energy_mwh": 0.98 + j * 0.0004})
        command = energy_return_command(d.devices, state, policy)
        actual = execute_tube_command(d.devices, state, command)
        assert tube_contains(proof.endpoint, actual)
        assert abs(actual.energy_mwh - 1) <= abs(state.energy_mwh - 1) + 1e-15


def test_disabled_storage_cannot_be_reported_as_a_joint_task_certificate():
    d = task_domain()
    resources = tuple(
        r.model_copy(update={"p": I(low=0, high=0), "ramp_p_mw": 0})
        if r.kind == "storage"
        else r
        for r in d.devices.resources
    )
    d = d.model_copy(
        update={"devices": d.devices.model_copy(update={"resources": resources})}
    )
    initial = TubeState(p_mw=(0, 0, 0), q_mvar=(0, 0, 0), energy_mwh=1.1)
    result = run_unified_feedback(
        d,
        initial,
        total_steps=30,
        update_steps=5,
        initial_target=(3, 0),
        observe=lambda k: observation(d, k),
        propose=unavailable,
    )
    assert result.status == "passed" and result.hard_task.guarantee == "unproven"
    assert result.hard_task.outcome == "missed"


def test_tampered_task_claim_is_rejected_by_replay():
    d = task_domain(deadline=1)
    result = run_unified_feedback(
        d,
        TubeState(p_mw=(0, 0, 0), q_mvar=(0, 0, 0), energy_mwh=1.4),
        total_steps=2,
        update_steps=1,
        initial_target=(3, 0),
        observe=lambda k: observation(d, k),
        propose=unavailable,
    )
    altered = result.model_copy(
        update={
            "hard_task": result.hard_task.model_copy(update={"guarantee": "maintained"})
        }
    )
    with pytest.raises(ValueError, match="hard task assessment"):
        verify_unified_execution(altered)


def test_energy_at_deadline_is_not_replaced_by_later_safe_tail_energy():
    from oilfield_energy.modules.control.contracts import TubeCommand
    from oilfield_energy.workflows.single_execution.api import certify_unified_plan

    d = task_domain(deadline=1)
    d = d.model_copy(
        update={
            "energy_task": d.energy_task.model_copy(
                update={"energy_mwh": I(low=1.009, high=1.010)}
            )
        }
    )
    state = TubeState(p_mw=(0, 0, 0.4), q_mvar=(0, 0, 0), energy_mwh=1.018)
    plan = certify_unified_plan(
        d, point_box(state), 0, (TubeCommand(p_mw=(0, 0, 0.5), q_mvar=(0, 0, 0)),)
    )
    assert (
        1.009
        <= plan.task_continuation.deadline_energy_mwh.low
        <= plan.task_continuation.deadline_energy_mwh.high
        <= 1.010
    )
    assert plan.terminal.energy_mwh.high < 1.009


@pytest.mark.parametrize("scale", [0.1, 10])
def test_joint_continuation_scales_with_equipment_and_energy_not_site_constants(scale):
    d = task_domain()
    def interval(value):
        return I(low=value.low * scale, high=value.high * scale)
    devices = d.devices.model_copy(
        update={
            "resources": tuple(
                r.model_copy(
                    update={
                        "p": interval(r.p),
                        "q": interval(r.q),
                        "s_mva": r.s_mva * scale,
                        "ramp_p_mw": r.ramp_p_mw * scale,
                    }
                )
                for r in d.devices.resources
            ),
            "energy_mwh": interval(d.devices.energy_mwh),
            "aggregate_q_ramp_mvar": d.devices.aggregate_q_ramp_mvar * scale,
        }
    )
    network = d.network.model_copy(
        update={
            "base_mva": d.network.base_mva * scale,
            "branches": tuple(
                b.model_copy(update={"rating_mva": b.rating_mva * scale})
                for b in d.network.branches
            ),
            "pcc_min_mw": d.network.pcc_min_mw * scale,
            "pcc_max_mw": d.network.pcc_max_mw * scale,
        }
    )
    forecasts = tuple(
        f.model_copy(
            update={
                "loads": tuple(
                    b.model_copy(
                        update={"p_mw": interval(b.p_mw), "q_mvar": interval(b.q_mvar)}
                    )
                    for b in f.loads
                ),
                "generation": tuple(
                    g.model_copy(
                        update={"minimum_available_mw": g.minimum_available_mw * scale}
                    )
                    for g in f.generation
                ),
            }
        )
        for f in d.forecasts
    )
    d = d.model_copy(
        update={
            "devices": devices,
            "network": network,
            "forecasts": forecasts,
            "energy_task": d.energy_task.model_copy(
                update={"energy_mwh": interval(d.energy_task.energy_mwh)}
            ),
        }
    )
    initial = TubeState(p_mw=(0, 0, 0), q_mvar=(0, 0, 0), energy_mwh=1.06 * scale)
    result = run_unified_feedback(
        d,
        initial,
        total_steps=30,
        update_steps=5,
        initial_target=(3 * scale, 0),
        observe=lambda k: observation(d, k),
        propose=unavailable,
    )
    assert (
        result.hard_task.guarantee == "maintained" and result.hard_task.outcome == "met"
    )
    verify_unified_execution(result)

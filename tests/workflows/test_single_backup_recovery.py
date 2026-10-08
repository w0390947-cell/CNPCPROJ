"""Actual AC fallback, original task preservation and whole-tail retry."""

import pytest

from oilfield_energy.kernel.intervals import Interval as I
from oilfield_energy.modules.control.api import (
    backup_command_candidates,
    certify_device_transition,
    point_box,
)
from oilfield_energy.modules.control.contracts import TubeCommand, TubeState
from oilfield_energy.workflows.single_execution.api import (
    certify_unified_plan,
    certify_unified_terminal,
)
from oilfield_energy.workflows.single_execution.contracts import (
    FirmGeneration,
    TerminalEnergyTask,
)
from oilfield_energy.workflows.single_execution.network import unified_network_box
from tests.unit.control.test_device_tube import model
from tests.workflows.test_unified_single import tracking_domain


def recovery_domain(*, reactive=True):
    d = tracking_domain()
    svg = d.devices.resources[-1].model_copy(
        update={"q": I(low=-2, high=2), "s_mva": 2}
    )
    devices = model().model_copy(
        update={
            "alpha_q": 0.5,
            "resources": (
                *tuple(r.model_copy(update={"bus": "load"}) for r in model().resources),
                svg,
            ),
        }
    )
    return d.model_copy(
        update={
            "devices": devices,
            "context_id": devices.context_id,
            "network": d.network.model_copy(update={"pf_min": 0.95}),
            "forecasts": tuple(
                f.model_copy(
                    update={
                        "generation": (
                            FirmGeneration(resource_id="w", minimum_available_mw=3),
                        ),
                        "loads": (
                            f.loads[0],
                            f.loads[1].model_copy(
                                update={
                                    "q_mvar": I(
                                        low=1.6 if reactive else 0,
                                        high=1.6 if reactive else 0,
                                    )
                                }
                            ),
                        ),
                    }
                )
                for f in d.forecasts
            ),
            "terminal_command": TubeCommand(
                p_mw=(0, 0, 0), q_mvar=(0, 0, 1.6 if reactive else 0)
            ),
        }
    )


def test_full_and_half_ac_reject_then_quarter_stops_pcs_and_certifies_complete_tail():
    d = recovery_domain()
    source = point_box(
        TubeState(p_mw=(2, 0.2, 0), q_mvar=(0.5, 0, 1.3), energy_mwh=0.2)
    )
    assert certify_unified_terminal(d)
    for fraction in (1, 0.5):
        candidate = backup_command_candidates(
            d.devices, source, d.terminal_command, movement_fraction=fraction
        )[0]
        transition = certify_device_transition(d.devices, source, candidate)
        assert not unified_network_box(
            d, transition.path, d.forecasts[0], reusable=True
        ).certified
    scaled = backup_command_candidates(
        d.devices,
        source,
        d.terminal_command,
        movement_fraction=0.25,
        independent_pcs=False,
    )[0]
    with pytest.raises(ValueError, match="storage energy envelope"):
        certify_device_transition(d.devices, source, scaled)
    plan = certify_unified_plan(d, source, 0)
    assert plan.commands[0].p_mw == (1.5, 0, 0)
    assert plan.terminal.energy_mwh == source.energy_mwh
    assert len(plan.commands) > 1


def test_network_can_require_continued_charging_instead_of_immediate_zero():
    d = recovery_domain(reactive=False)
    d = d.model_copy(
        update={
            "network": d.network.model_copy(update={"pcc_min_mw": 0.05}),
            "forecasts": tuple(
                f.model_copy(
                    update={
                        "loads": (
                            f.loads[0],
                            f.loads[1].model_copy(update={"p_mw": I(low=2, high=2.02)}),
                        )
                    }
                )
                for f in d.forecasts
            ),
        }
    )
    source = point_box(TubeState(p_mw=(2, -0.2, 0), q_mvar=(0, 0, 0), energy_mwh=1))
    assert certify_unified_terminal(d)
    plan = certify_unified_plan(d, source, 0)
    assert plan.commands[0].p_mw[1] < 0
    assert plan.commands[-1].p_mw[1] == 0


def test_failed_joint_task_retries_the_entire_scaled_tail():
    d = recovery_domain(reactive=False)
    d = d.model_copy(
        update={
            "network": d.network.model_copy(update={"pcc_min_mw": 0.01}),
            "energy_task": TerminalEnergyTask(
                task_id="original",
                deadline_step=1,
                energy_mwh=I(low=0.9996, high=0.99965),
                source="analytic one-cycle task",
            ),
        }
    )
    source = point_box(TubeState(p_mw=(2, 0.9, 0), q_mvar=(0, 0, 0), energy_mwh=1))
    plan = certify_unified_plan(d, source, 0)
    # Immediate zero is locally safe but misses the original one-cycle task.
    # The retained scaled construction supplies ~0.018 MW and meets it.
    assert plan.commands[0].p_mw[1] == pytest.approx(0.018)
    assert plan.task_continuation is not None
    assert plan.task_continuation.task == d.energy_task
    assert plan.task_continuation.deadline_energy_mwh.low >= 0.9996
    assert plan.task_continuation.deadline_energy_mwh.high <= 0.99965

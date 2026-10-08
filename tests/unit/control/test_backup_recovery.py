"""Independent PCS stopping, exact ramp boundaries and robust energy reserves."""

import pytest

from oilfield_energy.kernel.intervals import Interval as I
from oilfield_energy.modules.control.api import (
    backup_command_candidates,
    certify_device_transition,
    point_box,
)
from oilfield_energy.modules.control.contracts import TubeCommand, TubeState
from tests.unit.control.test_device_tube import model


@pytest.mark.parametrize("fraction", (1, 0.5, 0.25, 0.125, 0.0625))
@pytest.mark.parametrize("sign", (-1, 1))
def test_stop_at_energy_boundary_is_independent_of_other_resource_fraction(
    fraction, sign
):
    m = model().model_copy(update={"alpha_q": 0.5})
    energy = m.energy_mwh.low if sign > 0 else m.energy_mwh.high
    source = point_box(
        TubeState(p_mw=(2, sign * 0.2), q_mvar=(0.58, 0), energy_mwh=energy)
    )
    target = TubeCommand(p_mw=(0, 0), q_mvar=(0, 0))
    command = backup_command_candidates(m, source, target, movement_fraction=fraction)[
        0
    ]
    transition = certify_device_transition(m, source, command)
    assert command.p_mw[1] == 0
    assert transition.endpoint.energy_mwh == source.energy_mwh
    assert source.p_mw[1].low == sign * 0.2


def test_exact_ramp_boundary_does_not_force_an_unnecessary_energy_loss():
    m = model().model_copy(update={"alpha_q": 0.5})
    source = point_box(TubeState(p_mw=(2, 0.9), q_mvar=(0, 0), energy_mwh=0.2))
    command = backup_command_candidates(
        m, source, TubeCommand(p_mw=(0, 0), q_mvar=(0, 0))
    )[0]
    assert command.p_mw[1] == 0
    assert certify_device_transition(m, source, command).endpoint.energy_mwh.low == 0.2


@pytest.mark.parametrize("sign", (-1, 1))
def test_source_interval_requires_a_ramp_safe_command_for_every_state(sign):
    m = model().model_copy(update={"alpha_q": 0.5})
    storage = m.resources[1].model_copy(update={"ramp_p_mw": 0.25})
    m = m.model_copy(update={"resources": (m.resources[0], storage)})
    source = point_box(TubeState(p_mw=(2, sign * 0.5), q_mvar=(0, 0), energy_mwh=1))
    source = source.model_copy(
        update={
            "p_mw": (
                source.p_mw[0],
                I(low=0.4, high=0.6) if sign > 0 else I(low=-0.6, high=-0.4),
            )
        }
    )
    command = backup_command_candidates(
        m, source, TubeCommand(p_mw=(0, 0), q_mvar=(0, 0)), movement_fraction=0.25
    )[0]
    assert command.p_mw[1] == pytest.approx(sign * 0.35)
    certify_device_transition(m, source, command)


@pytest.mark.parametrize("sign", (-1, 1))
def test_energy_needed_after_the_first_braking_step_is_not_ignored(sign):
    m = model().model_copy(update={"alpha_q": 0.5})
    storage = m.resources[1].model_copy(update={"ramp_p_mw": 0.25})
    m = m.model_copy(update={"resources": (m.resources[0], storage)})
    energy = 0.212 if sign > 0 else 1.991
    source = point_box(
        TubeState(p_mw=(2, sign * 0.75), q_mvar=(0, 0), energy_mwh=energy)
    )
    target = TubeCommand(p_mw=(0, 0), q_mvar=(0, 0))
    candidates = backup_command_candidates(m, source, target)
    # 0.5 MW fits this minute (0.0104167 MWh), but 0.5 + 0.25 MW
    # requires 0.015625 MWh before stopping. No independent witness is offered.
    # Charging analog: 0.0075 MWh fits, but the full stop needs 0.01125 MWh.
    assert candidates == backup_command_candidates(
        m, source, target, independent_pcs=False
    )


def test_disabled_storage_with_zero_ramp_remains_exactly_zero():
    m = model().model_copy(update={"alpha_q": 0.5})
    storage = m.resources[1].model_copy(update={"p": I(low=0, high=0), "ramp_p_mw": 0})
    m = m.model_copy(update={"resources": (m.resources[0], storage)})
    source = point_box(TubeState(p_mw=(2, 0), q_mvar=(0, 0), energy_mwh=0.2))
    command = backup_command_candidates(
        m, source, TubeCommand(p_mw=(0, 0), q_mvar=(0, 0)), movement_fraction=0.25
    )[0]
    assert command.p_mw[1] == 0
    assert certify_device_transition(m, source, command).endpoint.energy_mwh.low == 0.2


def test_nonzero_economic_pcs_target_is_not_silently_replaced_by_zero():
    m = model()
    source = point_box(TubeState(p_mw=(2, 0.2), q_mvar=(0, 0), energy_mwh=1))
    with pytest.raises(ValueError, match="zero PCS terminal"):
        backup_command_candidates(m, source, TubeCommand(p_mw=(0, 0.1), q_mvar=(0, 0)))

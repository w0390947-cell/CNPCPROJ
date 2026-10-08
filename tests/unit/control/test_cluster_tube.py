"""Independent energy integral and physical-model separation checks."""

from fractions import Fraction as F

import pytest

from oilfield_energy.modules.control.api import (
    certify_cluster_device_transition,
    execute_cluster_device_command,
    certify_device_transition,
    point_box,
    tube_contains,
)
from oilfield_energy.modules.control.contracts import TubeCommand, TubeState
from tests.support.cluster_safety import safety_domain


def test_reversal_energy_integral_covers_interior_extremum():
    r = safety_domain(reserve=False).regions[0]
    model = r.devices.model_copy(
        update={
            "resources": tuple(
                x.model_copy(update={"alpha_p": 1.0, "ramp_p_mw": 3.0})
                for x in r.devices.resources
            )
        }
    )
    i = next(i for i, x in enumerate(model.resources) if x.kind == "storage")
    p = [0.0] * len(model.resources)
    p[i] = 1.0
    state = TubeState(p_mw=tuple(p), q_mvar=r.initial.q_mvar, energy_mwh=2.0)
    p[i] = -1.0
    command = TubeCommand(p_mw=tuple(p), q_mvar=state.q_mvar)
    proof = certify_cluster_device_transition(model, point_box(state), command)
    result = execute_cluster_device_command(model, state, command)
    # Two triangles of area 1/4 MW minute with distinct efficiencies.
    expected = F(2) + (F(model.eta_charge) - 1 / F(model.eta_discharge)) / 240
    assert result.energy_mwh == float(expected)
    assert tube_contains(proof.endpoint, result)
    at_half = 2.0 - 1 / (240 * model.eta_discharge)
    assert at_half < min(state.energy_mwh, result.energy_mwh)
    assert proof.path.energy_mwh.contains(at_half)
    with pytest.raises(ValueError, match="ZOH"):
        certify_device_transition(model, point_box(state), command)


def test_storage_lag_and_linear_energy_are_both_applied():
    r = safety_domain(reserve=False).regions[0]
    i = next(i for i, x in enumerate(r.devices.resources) if x.kind == "storage")
    p = list(r.initial.p_mw)
    p[i] = -0.3
    result = execute_cluster_device_command(
        r.devices, r.initial, TubeCommand(p_mw=tuple(p), q_mvar=r.initial.q_mvar)
    )
    assert result.p_mw[i] == pytest.approx(-0.3 * r.devices.resources[i].alpha_p)
    assert result.energy_mwh == pytest.approx(
        2 - result.p_mw[i] * r.devices.eta_charge / 120
    )

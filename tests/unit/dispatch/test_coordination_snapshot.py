from dataclasses import FrozenInstanceError, replace

import pytest

from oilfield_energy.modules.dispatch.api import capture_coordination_snapshot
from oilfield_energy.modules.dispatch.contracts import DispatchCapabilities, RegionalPlanSnapshot


def plan(name, p):
    return RegionalPlanSnapshot(
        name,
        (p,),
        (0.0,),
        (0.0,),
        (0.0,),
        (0.0,),
        (1.0, 1.0),
        (0.0,),
        100.0,
        "optimal",
        2,
        False,
        None,
        None,
    )


def snapshot(**overrides):
    args = dict(
        epoch=2,
        communication_tick=10,
        capabilities=DispatchCapabilities(False),
        regions=(plan("a", 3.0), plan("b", 4.0)),
        p_references_mw=[[0.0], [0.0]],
        q_references_mvar=[[0.0], [0.0]],
        previous_p_references_mw=[[0.0], [1.0]],
        previous_q_references_mvar=[[0.0], [0.0]],
        dual_p=[[0.0], [0.0]],
        dual_q=[[0.0], [0.0]],
        rho=2.0,
        absolute_tolerance=0.1,
        relative_tolerance=0.01,
    )
    args.update(overrides)
    return capture_coordination_snapshot(**args)


def test_residuals_have_independent_analytic_answer_and_owned_arrays():
    source = [[0.0], [0.0]]
    item = snapshot(p_references_mw=source)
    source[0][0] = 100
    assert item.residuals.primal == 5.0
    assert item.residuals.dual == 2.0
    assert item.residuals.primal_tolerance == pytest.approx(0.25)
    assert item.residuals.dual_tolerance == pytest.approx(0.2)
    assert item.p_references_mw[0][0] == 0
    assert not item.converged
    with pytest.raises(FrozenInstanceError):
        item.epoch = 9


@pytest.mark.parametrize(
    "changes",
    [
        {"signal_iteration": 1},
        {"used_fallback": True},
        {"name": "a"},
        {"p_grid_mw": (float("nan"),)},
        {"storage_energy_mwh": (1.0,)},
        {"storage_charge_mw": (1.0,)},
        {"storage_energy_mwh": (1.0, 2.0)},
    ],
)
def test_invalid_or_mixed_evidence_rejected(changes):
    with pytest.raises(ValueError):
        snapshot(regions=(plan("a", 3.0), replace(plan("b", 4.0), **changes)))

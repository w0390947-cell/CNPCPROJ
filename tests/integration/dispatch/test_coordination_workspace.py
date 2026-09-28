"""Rolling reuse must refresh data, invalidate structure and preserve certificates."""

from dataclasses import replace
from unittest.mock import Mock

import cvxpy as cp
import numpy as np
import pytest

from oilfield_energy.admm import CoordinationWorkspace, run_admm_coordination
from oilfield_energy.bootstrap.adapters.cluster_rolling import (
    forecast_grid,
    window_case,
)
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig
from oilfield_energy.modules.dispatch.contracts import DispatchCapabilities
from oilfield_energy.planning_security import build_planning_security_trajectories
from oilfield_energy.regional_control import _installed_solvers, _solve_problem


def window():
    case = forecast_grid(build_synthetic_case(steps=24), 15)
    energies = {m.name: m.storage.e_initial_mwh for m in case.microgrids}
    return window_case(case, 0, 8, energies, energies)


def prepare(workspace, case, *, config=None, enabled=True, losses=None, floors=None):
    if floors is None:
        security = build_planning_security_trajectories(
            case, [m.name for m in case.microgrids]
        )
        floors = {n: v.effective_floor_mw for n, v in security.items()}
    return workspace.prepare(
        case,
        case.microgrids,
        config or ADMMConfig(),
        DispatchCapabilities(storage_enabled=enabled),
        floors,
        losses,
    )


def test_solver_discovery_is_cached_and_failure_still_uses_fallback(monkeypatch):
    _installed_solvers.cache_clear()
    discover = Mock(return_value=["CLARABEL", "OSQP"])
    monkeypatch.setattr(cp, "installed_solvers", discover)
    problem = Mock(status=cp.OPTIMAL)
    problem.solve.side_effect = [RuntimeError("backend unavailable"), None, None]
    try:
        _solve_problem(problem, "CLARABEL")
        _solve_problem(problem, "CLARABEL")
        assert discover.call_count == 1
        assert [c.kwargs["solver"] for c in problem.solve.call_args_list] == [
            "CLARABEL",
            "OSQP",
            "CLARABEL",
        ]
        problem.solve.side_effect = RuntimeError("all backends failed")
        with pytest.raises(RuntimeError, match="convex solver failed"):
            _solve_problem(problem, "CLARABEL")
    finally:
        _installed_solvers.cache_clear()


def test_all_window_parameters_refresh_without_rebuilding_or_mutating_prior_output():
    case = window()
    workspace = CoordinationWorkspace()
    controllers, projection = prepare(workspace, case)
    first = controllers["SC"].solve(None, fallback=True)
    old_p = first.p_grid_mw.copy()
    original_load = {
        bus: values.copy() for bus, values in case.microgrids[0].load_p_mw.items()
    }
    changed = replace(
        case,
        time_hours=case.time_hours + 0.25,
        price_cny_per_mwh=case.price_cny_per_mwh + 70,
        microgrids=[
            replace(
                m,
                load_p_mw={b: v * 1.03 for b, v in m.load_p_mw.items()},
                load_q_mvar={b: v * 0.98 for b, v in m.load_q_mvar.items()},
                wind_available_mw={b: v * 0.9 for b, v in m.wind_available_mw.items()},
                pv_available_mw={b: v + 0.1 for b, v in m.pv_available_mw.items()},
                storage=replace(
                    m.storage,
                    e_initial_mwh=m.storage.e_initial_mwh + 0.1,
                    e_terminal_mwh=m.storage.e_initial_mwh + 0.2,
                ),
            )
            for m in case.microgrids
        ],
    )
    losses = {
        m.name: {"p_loss_mw": np.full(8, 0.02), "q_loss_mvar": np.full(8, 0.01)}
        for m in changed.microgrids
    }
    floors = {m.name: np.full(8, 0.3) for m in changed.microgrids}
    warm, same_projection = prepare(workspace, changed, losses=losses, floors=floors)
    cold, _ = prepare(CoordinationWorkspace(), changed, losses=losses, floors=floors)
    assert warm["SC"] is controllers["SC"] and same_projection is projection
    for m in changed.microgrids:
        a, b = (
            warm[m.name].solve(None, fallback=True),
            cold[m.name].solve(None, fallback=True),
        )
        assert warm[m.name].problem.is_dpp()
        np.testing.assert_allclose(a.p_grid_mw, b.p_grid_mw, atol=2e-4)
        np.testing.assert_allclose(a.q_grid_mvar, b.q_grid_mvar, atol=2e-4)
        assert a.economic_cost.economic_cost_cny == pytest.approx(
            b.economic_cost.economic_cost_cny, abs=0.05
        )
        assert a.storage_energy_mwh[0] == pytest.approx(
            m.storage.e_initial_mwh, abs=1e-6
        )
        assert abs(a.storage_energy_mwh[-1] - m.storage.terminal_energy_mwh) <= 0.050001
    np.testing.assert_array_equal(first.p_grid_mw, old_p)
    for bus, value in original_load.items():
        np.testing.assert_array_equal(value, case.microgrids[0].load_p_mw[bus])
    np.testing.assert_array_equal(projection.lower.value, np.full((3, 8), 0.3))


@pytest.mark.parametrize(
    "change",
    ["horizon", "rating", "authorization", "mapping", "version", "config", "storage"],
)
def test_structural_changes_rebuild(change):
    case = window()
    workspace = CoordinationWorkspace()
    before, _ = prepare(workspace, case)
    kwargs = {}
    if change == "horizon":
        energies = {m.name: m.storage.e_initial_mwh for m in case.microgrids}
        case = window_case(case, 0, 1, energies, energies)
    elif change == "rating":
        m = case.microgrids[0]
        case = replace(
            case,
            microgrids=[
                replace(m, p_grid_max_mw=m.p_grid_max_mw + 1),
                *case.microgrids[1:],
            ],
        )
    elif change == "authorization":
        m = case.microgrids[0]
        case = replace(
            case,
            microgrids=[
                replace(m, storage_reactive_enabled=not m.storage_reactive_enabled),
                *case.microgrids[1:],
            ],
        )
    elif change == "mapping":
        case = replace(case, microgrids=list(reversed(case.microgrids)))
    elif change == "version":
        case = replace(case, dataset_revision="different-version")
    elif change == "config":
        kwargs["config"] = ADMMConfig(rho=130)
    else:
        kwargs["enabled"] = False
    after, _ = prepare(workspace, case, **kwargs)
    assert before["SC"] is not after["SC"]


def test_failed_refresh_invalidates_workspace():
    case = window()
    workspace = CoordinationWorkspace()
    before, _ = prepare(workspace, case)
    bad = {
        m.name: {
            "p_loss_mw": np.full(8, -1 if m.name == "YA_B" else 0.1),
            "q_loss_mvar": np.zeros(8),
        }
        for m in case.microgrids
    }
    with pytest.raises(ValueError, match="nonnegative"):
        prepare(workspace, case, losses=bad)
    after, _ = prepare(workspace, case)
    assert after["SC"] is not before["SC"]


def test_reserve_parameters_refresh_and_clear_on_reused_models():
    from oilfield_energy.bootstrap.adapters.cluster_reserve import with_storage_reserves
    from oilfield_energy.modules.dispatch.contracts import StorageReservePolicy

    case = window()
    workspace = CoordinationWorkspace()
    first, _ = prepare(workspace, case)
    reserved = with_storage_reserves(case, StorageReservePolicy(), 1440)
    warm, _ = prepare(workspace, reserved)
    cold, _ = prepare(CoordinationWorkspace(), reserved)
    for mg in reserved.microgrids:
        assert warm[mg.name] is first[mg.name]
        actual = warm[mg.name].solve(None, fallback=True)
        reference = cold[mg.name].solve(None, fallback=True)
        np.testing.assert_allclose(actual.p_grid_mw, reference.p_grid_mw, atol=2e-4)
        np.testing.assert_allclose(actual.storage_energy_mwh, reference.storage_energy_mwh, atol=2e-4)
        assert warm[mg.name].problem.is_dpp()
    cleared, _ = prepare(workspace, case)
    for mg in case.microgrids:
        assert cleared[mg.name] is first[mg.name]
        np.testing.assert_array_equal(cleared[mg.name].reserve_power_maximum.value, np.full(8, mg.storage.p_max_mw))


def test_reactive_initial_state_refreshes_without_rebuilding_or_stale_constraints():
    from oilfield_energy.bootstrap.adapters.cluster_reactive import with_reactive_planning
    from oilfield_energy.modules.control.contracts import DeviceCheckpoint, DynamicTrackingPolicy
    from oilfield_energy.modules.dispatch.contracts import ReactivePlanningPolicy

    case = window()
    workspace = CoordinationWorkspace()
    def planned(q):
        return with_reactive_planning(case, policy=ReactivePlanningPolicy(), dynamics=DynamicTrackingPolicy(),
            checkpoints={m.name: DeviceCheckpoint(minute=15, storage_energy_mwh=m.storage.e_initial_mwh,
                storage_power_mw=0., reactive_power_mvar={r.resource_id:q for r in m.resource_identities}) for m in case.microgrids}, storage_enabled=True)
    before, _ = prepare(workspace, planned(0.))
    changed = planned(.1)
    warm, _ = prepare(workspace, changed)
    cold, _ = prepare(CoordinationWorkspace(), changed)
    for name in warm:
        assert warm[name] is before[name]
        a, b = warm[name].solve(None, fallback=True), cold[name].solve(None, fallback=True)
        np.testing.assert_allclose(a.q_grid_mvar, b.q_grid_mvar, atol=2e-4)
    without, _ = prepare(workspace, case)
    assert without['SC'] is not before['SC']


def test_reused_admm_starts_new_protocol_and_matches_cold_reference():
    case = window()
    workspace = CoordinationWorkspace()
    config = ADMMConfig(max_iterations=220)
    first = run_admm_coordination(case, workspace=workspace, admm_config=config)
    frozen = first.coordination_snapshot
    changed = replace(case, price_cny_per_mwh=case.price_cny_per_mwh + 10)
    warm = run_admm_coordination(changed, workspace=workspace, admm_config=config)
    cold = run_admm_coordination(changed, admm_config=config)
    assert warm.converged and cold.converged
    assert warm.history[0].coordination.coordination_epoch == 0
    assert warm.history[0].iteration == 1
    for name in warm.p_references_mw:
        np.testing.assert_allclose(
            warm.p_references_mw[name], cold.p_references_mw[name], atol=2e-3
        )
        np.testing.assert_allclose(
            warm.q_references_mvar[name], cold.q_references_mvar[name], atol=2e-3
        )
    assert first.coordination_snapshot == frozen

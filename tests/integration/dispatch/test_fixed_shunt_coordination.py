"""Synthetic three-region shunt regression with real ADMM, MISOCP and AC checks."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.admm import CoordinationWorkspace, run_admm_coordination
from oilfield_energy.bootstrap.adapters.cluster_execution import (
    RegionalPlanRequest,
    realize_regional_plan,
)
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.hierarchy_types import ADMMConfig, CommunicationConfig
from oilfield_energy.modules.dispatch.api import build_coordination_calibration
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    DispatchCapabilities,
    FixedShuntReference,
)
from oilfield_energy.network_model import (
    NetworkOperatingMode,
    ShuntCompensator,
    ShuntKind,
    ShuntOperatingPoint,
)
from oilfield_energy.regional_control import RegionalConvexController
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy


def fixed_case(kind=ShuntKind.CAPACITOR, *, enabled=True, steps=1):
    """Existing radial networks; no adjustable resources can hide a Q error."""
    base = build_synthetic_case(4)
    regions = []
    for original in base.microgrids:
        load = {b: np.full(steps, v[0]) for b, v in original.load_p_mw.items()}
        network = original.network_model_v2
        mode_id = original.network_operating_mode_id
        if original.name == "SC":
            shunt = ShuntCompensator("AUDIT_SHUNT", original.svg_bus, kind, 0.1, 0, 4, 0)
            mode = NetworkOperatingMode(
                "shunt-test",
                "Synthetic fixed compensation",
                shunt_operating_points=(ShuntOperatingPoint(shunt.shunt_id, enabled, 2),),
            )
            network = replace(
                network,
                shunts=(shunt,),
                operating_modes=(mode,),
                default_operating_mode_id=mode.mode_id,
            )
            mode_id = mode.mode_id
        regions.append(
            replace(
                original,
                load_p_mw=load,
                load_q_mvar={b: 0.1 * v for b, v in load.items()},
                wind_available_mw={b: np.zeros(steps) for b in original.wind_available_mw},
                pv_available_mw={b: np.zeros(steps) for b in original.pv_available_mw},
                wind_q_abs_max_mvar={b: 0 for b in original.wind_capacity_mw},
                pv_reactive_enabled={b: False for b in original.pv_available_mw},
                storage_reactive_enabled=False,
                svg_q_min_mvar=0,
                svg_q_max_mvar=0,
                network_model_v2=network,
                network_operating_mode_id=mode_id,
            )
        )
    return replace(
        base,
        microgrids=regions,
        time_hours=np.arange(steps) / 4,
        price_cny_per_mwh=base.price_cny_per_mwh[:steps],
        assumptions=replace(base.assumptions, dt_hours=0.25),
    )


def calibration_from(result):
    return {
        name: build_coordination_calibration(
            p_loss_mw=row["loss_mw"],
            q_loss_mvar=row["reactive_loss_mvar"],
            shunt_nominal_mvar_by_bus=row["fixed_shunt_q_nominal_mvar_by_bus"],
            shunt_q_mvar_by_bus=row["fixed_shunt_q_mvar_by_bus"],
        )
        for name, row in result.microgrids.items()
    }


@pytest.mark.parametrize(
    "kind,enabled",
    [(ShuntKind.CAPACITOR, True), (ShuntKind.REACTOR, True), (ShuntKind.CAPACITOR, False)],
)
def test_shunt_reference_preserves_physical_q_and_passes_unchanged_adoption_checks(kind, enabled):
    case = fixed_case(kind, enabled=enabled)
    physical = solve_case_ac_consistent(case, storage_enabled=False, time_limit_seconds=20)
    assert physical.passed, physical.stop_reason
    calibrations = calibration_from(physical.optimization)
    reference = run_admm_coordination(
        case,
        storage_enabled=False,
        loss_calibration=calibrations,
        admm_config=ADMMConfig(max_iterations=400),
    )
    assert reference.converged, reference.stop_reason
    for mg in case.microgrids:
        row = physical.optimization.microgrids[mg.name]
        # Independent bus conservation, including actual V^2, not another
        # invocation of the coordinator's demand-construction function.
        shunt = sum(row["fixed_shunt_q_mvar_by_bus"].values())
        expected = sum(mg.load_q_mvar.values()) + row["reactive_loss_mvar"] - shunt
        np.testing.assert_allclose(row["q_grid_mvar"], expected, atol=1e-6, rtol=0)
        np.testing.assert_allclose(
            reference.q_references_mvar[mg.name], expected, atol=1e-5, rtol=0
        )
        regional, plan = realize_regional_plan(
            RegionalPlanRequest(
                case,
                mg.name,
                reference.p_references_mw[mg.name],
                reference.q_references_mvar[mg.name],
                False,
                20,
                ExecutionPolicy(),
            )
        )
        assert plan is not None and regional.status == "passed", regional
    sc = case.microgrids[0]
    row = physical.optimization.microgrids[sc.name]
    voltage = row["voltage_pu"][sc.buses.index(sc.svg_bus)]
    nominal = (0.2 if kind is ShuntKind.CAPACITOR else -0.2) if enabled else 0
    np.testing.assert_allclose(
        row["fixed_shunt_q_mvar_by_bus"][sc.svg_bus], nominal * voltage**2, atol=1e-10, rtol=0
    )
    print(
        f"{kind.value}, enabled={enabled}: Qphysical={row['q_grid_mvar']}, "
        f"Qreference={reference.q_references_mvar[sc.name]}, shunt="
        f"{row['fixed_shunt_q_mvar_by_bus'][sc.svg_bus]}"
    )


@pytest.mark.parametrize("legacy", [None, {"p_loss_mw": np.zeros(1), "q_loss_mvar": np.zeros(1)}])
def test_active_shunt_without_its_calibration_is_rejected(legacy):
    case = fixed_case()
    with pytest.raises(ValueError, match="active fixed shunts require"):
        RegionalConvexController(case, case.microgrids[0], ADMMConfig(), loss_calibration=legacy)


def test_inactive_shunt_keeps_legacy_loss_only_calls_compatible():
    case = fixed_case(enabled=False)
    mg = case.microgrids[0]
    ctrl = RegionalConvexController(
        case,
        mg,
        ADMMConfig(),
        loss_calibration={"p_loss_mw": np.zeros(1), "q_loss_mvar": np.zeros(1)},
    )
    np.testing.assert_allclose(ctrl.q_demand.value, sum(mg.load_q_mvar.values()))


def test_reused_controller_refreshes_shunt_values_and_clears_them_after_switching_off():
    case = fixed_case(steps=2)
    workspace = CoordinationWorkspace()
    floors = {m.name: np.full(2, 0.05) for m in case.microgrids}
    losses = {m.name: CoordinationNetworkCalibration((0, 0), (0, 0)) for m in case.microgrids}
    bus = case.microgrids[0].svg_bus
    losses["SC"] = CoordinationNetworkCalibration(
        (0, 0), (0, 0), (FixedShuntReference(bus, 0.2, (0.18, 0.19)),)
    )

    def prepare(current):
        return workspace.prepare(
            current,
            current.microgrids,
            ADMMConfig(),
            DispatchCapabilities(storage_enabled=False),
            floors,
            losses,
        )[0]

    before = prepare(case)
    first_q = before["SC"].q_demand.value.copy()
    losses["SC"] = replace(losses["SC"], fixed_shunts=(FixedShuntReference(bus, 0.2, (0.2, 0.21)),))
    after = prepare(case)
    assert after["SC"] is before["SC"]
    np.testing.assert_allclose(after["SC"].q_demand.value, first_q - 0.02, atol=1e-12)
    assert after["SC"].problem.is_dpp()
    off = fixed_case(enabled=False, steps=2)
    with pytest.raises(ValueError, match="operating mode"):
        prepare(off)
    losses["SC"] = CoordinationNetworkCalibration((0, 0), (0, 0))
    cleared = prepare(off)
    assert cleared["SC"] is not after["SC"]
    np.testing.assert_array_equal(cleared["SC"].fixed_shunt_q_mvar, np.zeros(2))


@pytest.mark.parametrize("entry", ["service", "study"])
def test_all_entry_routes_deliver_typed_shunt_evidence(monkeypatch, entry, tmp_path):
    """Stop at the coordination boundary; do not run a device simulation."""
    import oilfield_energy.service as service
    from oilfield_energy.bootstrap.adapters.cluster_study import run_cluster_study

    case = fixed_case(steps=4)
    checked = solve_case_ac_consistent(case, storage_enabled=False, time_limit_seconds=20)
    assert checked.passed
    captured = {}

    class ReachedCoordination(Exception):
        pass

    def receive(*args, **kwargs):
        captured.update(kwargs["loss_calibration"])
        raise ReachedCoordination

    monkeypatch.setattr(service, "_solve", lambda *a, **kw: (checked.optimization, {}))
    monkeypatch.setattr(service, "run_admm_coordination", receive)
    request = service.SimulationRequest(scenario_type="cluster_coordination", steps=4, storage_enabled=False)
    with pytest.raises(ReachedCoordination):
        if entry == "service":
            service.run_simulation(request, input_case=case)
        else:
            run_cluster_study(request, tmp_path / "study", input_case=case)
    assert captured == calibration_from(checked.optimization)
    assert captured["SC"].fixed_shunts[0].nominal_q_mvar == pytest.approx(0.2)


def test_regular_and_minute_rolling_windows_pass_aligned_shunt_calibration(monkeypatch):
    """Exercise real window preparation and coordination, stopping before devices."""
    from oilfield_energy.bootstrap.adapters import cluster_rolling

    case = fixed_case(steps=2)
    physical = solve_case_ac_consistent(case, storage_enabled=False, time_limit_seconds=20)
    assert physical.passed
    calibration = calibration_from(physical.optimization)
    # Different reference voltages distinguish source intervals without changing
    # the physical network. This test assesses data alignment, not realizability.
    sc = calibration["SC"]
    calibration["SC"] = replace(
        sc, fixed_shunts=(replace(sc.fixed_shunts[0], q_mvar=(0.19, 0.21)),)
    )
    original = run_admm_coordination(case, storage_enabled=False, loss_calibration=calibration)
    assert original.converged
    day_ahead = tuple(
        realize_regional_plan(
            RegionalPlanRequest(
                case,
                m.name,
                physical.optimization.microgrids[m.name]["p_grid_mw"],
                physical.optimization.microgrids[m.name]["q_grid_mvar"],
                False,
                20,
                ExecutionPolicy(),
            )
        )[0]
        for m in case.microgrids
    )
    assert all(r.status == "passed" for r in day_ahead)
    received = []

    def coordinate(window, *args, **kwargs):
        received.append((window.time_hours.copy(), kwargs["loss_calibration"]["SC"]))
        solved = run_admm_coordination(window, *args, **kwargs)
        assert solved.converged
        # Deliberate stop after proving the bridge reaches the real coordinator.
        return replace(solved, converged=False)

    monkeypatch.setattr(cluster_rolling, "run_admm_coordination", coordinate)
    engine = cluster_rolling.RollingClusterEngine(
        case=case,
        original_case=case,
        plants={},
        original_admm=original,
        day_ahead=day_ahead,
        storage_enabled=False,
        policy=ExecutionPolicy(coordination_model_reuse=True),
        admm_config=ADMMConfig(),
        communication_config=CommunicationConfig(),
        loss_calibration=calibration,
        solve_regions=lambda *args: {},
        progress=lambda _: None,
    )
    engine.prepare(15, 30, 30)
    # A short replan normally inherits this from an adopted regular window.
    # This bridge test stopped before adoption, so supply that prerequisite.
    engine.current_terminal = dict(engine.energies)
    engine.prepare(7, 15, 30, minute_replan=True)
    assert len(received) == 2
    for times, row in received:
        indices = np.floor(times / 0.25).astype(int)
        np.testing.assert_array_equal(row.fixed_shunts[0].q_mvar, np.array([0.19, 0.21])[indices])
        np.testing.assert_array_equal(row.q_loss_mvar, np.asarray(sc.q_loss_mvar)[indices])
    assert received[1][0][0] == pytest.approx(7 / 60)
    assert received[0][1].fixed_shunts[0].q_mvar == (0.21,)

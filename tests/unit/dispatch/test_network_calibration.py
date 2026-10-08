"""Analytic signed-injection and immutable calibration contracts; no solvers."""

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from oilfield_energy.modules.dispatch.api import (
    build_coordination_calibration,
    coordination_shunt_injection,
    slice_coordination_calibration,
)
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    FixedShuntReference,
)


def test_signed_injections_are_summed_by_bus_and_copied_without_netting_losses():
    cap = [0.2 * 0.95**2, 0.2 * 1.05**2]
    reactor = [-0.1, -0.1]
    p, q = [0.01, 0.02], [0.03, 0.04]
    reference = build_coordination_calibration(
        p_loss_mw=p,
        q_loss_mvar=q,
        shunt_nominal_mvar_by_bus={"C": 0.2, "R": -0.1, "OFF": 0},
        shunt_q_mvar_by_bus={"OFF": [0, 0], "R": reactor, "C": cap},
    )
    cap[0] = p[0] = q[0] = 100
    assert reference.p_loss_mw == (0.01, 0.02)
    assert reference.q_loss_mvar == (0.03, 0.04)
    assert coordination_shunt_injection(
        reference, current_nominal_mvar_by_bus={"R": -0.1, "C": 0.2}, steps=2
    ) == pytest.approx((0.0805, 0.1205), abs=1e-12)
    with pytest.raises(FrozenInstanceError):
        reference.p_loss_mw = (1.0, 1.0)


@pytest.mark.parametrize("nominal", [{"C": 0.1}, {"C": -0.2}, {}, {"other": 0.2}])
def test_changed_step_sign_service_state_or_bus_rejects_stale_calibration(nominal):
    reference = CoordinationNetworkCalibration(
        (0.0,), (0.0,), (FixedShuntReference("C", 0.2, (0.19,)),)
    )
    with pytest.raises(ValueError, match="operating mode"):
        coordination_shunt_injection(reference, current_nominal_mvar_by_bus=nominal, steps=1)


def test_missing_reference_is_zero_only_for_no_resolved_compensation():
    assert coordination_shunt_injection(None, current_nominal_mvar_by_bus={"OFF": 0}, steps=2) == (
        0,
        0,
    )
    with pytest.raises(ValueError, match="require explicit"):
        coordination_shunt_injection(None, current_nominal_mvar_by_bus={"C": 0.2}, steps=2)
    with pytest.raises(ValueError, match="operating mode"):
        coordination_shunt_injection(
            CoordinationNetworkCalibration((0.0,), (0.0,)),
            current_nominal_mvar_by_bus={"C": 0.2},
            steps=1,
        )


@pytest.mark.parametrize("losses", [(float("nan"),), (float("inf"),), (-0.01,), ()])
def test_invalid_losses_are_rejected(losses):
    with pytest.raises(ValueError):
        CoordinationNetworkCalibration(losses, losses)


@pytest.mark.parametrize("actual", [(-0.19,), (0.0,), (float("nan"),), (float("inf"),), (0.2, 0.2)])
def test_invalid_voltage_dependent_injection_is_rejected(actual):
    with pytest.raises(ValueError):
        build_coordination_calibration(
            p_loss_mw=(0,),
            q_loss_mvar=(0,),
            shunt_nominal_mvar_by_bus={"C": 0.2},
            shunt_q_mvar_by_bus={"C": actual},
        )


@pytest.mark.parametrize(
    "nominal,actual",
    [
        ({"C": 0.2}, None),
        (None, {"C": [0.2]}),
        ({"C": 0.2}, {"X": [0.2]}),
        ({"C": 0}, {"C": [0.2]}),
    ],
)
def test_missing_misaligned_or_zero_nominal_data_is_not_silently_filled(nominal, actual):
    with pytest.raises(ValueError):
        build_coordination_calibration(
            p_loss_mw=(0,),
            q_loss_mvar=(0,),
            shunt_nominal_mvar_by_bus=nominal,
            shunt_q_mvar_by_bus=actual,
        )


def test_rolling_selection_keeps_losses_and_shunts_on_the_same_held_interval():
    reference = CoordinationNetworkCalibration(
        (0.01, 0.02, 0.03),
        (0.04, 0.05, 0.06),
        (FixedShuntReference("C", 0.2, (0.19, 0.2, 0.21)),),
    )
    window = slice_coordination_calibration(reference, (1, 1, 2))
    assert window.p_loss_mw == (0.02, 0.02, 0.03)
    assert window.q_loss_mvar == (0.05, 0.05, 0.06)
    assert window.fixed_shunts[0].q_mvar == (0.2, 0.2, 0.21)
    assert reference.fixed_shunts[0].q_mvar == (0.19, 0.2, 0.21)


@pytest.mark.parametrize("indices", [(), (-1,), (1,), (0.5,)])
def test_bad_window_indices_are_rejected(indices):
    with pytest.raises(ValueError):
        slice_coordination_calibration(CoordinationNetworkCalibration((0,), (0,)), indices)


def test_duplicate_buses_and_mismatched_horizons_are_rejected():
    shunt = FixedShuntReference("C", 0.2, (0.2,))
    with pytest.raises(ValueError):
        CoordinationNetworkCalibration((0,), (0,), (shunt, shunt))
    with pytest.raises(ValueError):
        CoordinationNetworkCalibration((0, 0), (0, 0), (shunt,))
    with pytest.raises(ValueError, match="horizon"):
        coordination_shunt_injection(
            CoordinationNetworkCalibration((0,), (0,)),
            current_nominal_mvar_by_bus={},
            steps=2,
        )


@pytest.mark.parametrize("field", ["p", "q", "shunt"])
def test_column_arrays_cannot_be_silently_coerced_into_scalar_time_series(field):
    # float(array([x])) can coerce the wrong axis shape on some NumPy versions.
    wrong = np.array([[0.1], [0.2]])
    with pytest.raises(ValueError):
        build_coordination_calibration(
            p_loss_mw=wrong if field == "p" else (0.1, 0.2),
            q_loss_mvar=wrong if field == "q" else (0.1, 0.2),
            shunt_nominal_mvar_by_bus={"C": 0.2},
            shunt_q_mvar_by_bus={"C": wrong if field == "shunt" else (0.2, 0.2)},
        )


def test_recovery_stagnation_compares_net_reactive_correction_and_physical_mode():
    from oilfield_energy.modules.dispatch.api import equivalent_coordination_calibration
    from oilfield_energy.modules.dispatch.contracts import CoordinationRecoveryPolicy

    policy = CoordinationRecoveryPolicy()
    left = CoordinationNetworkCalibration((0.1,), (0.3,), (FixedShuntReference("C", 0.2, (0.2,)),))
    # Series Q loss and shunt injection both grow 0.01: coordination still sees
    # the same 0.1 Mvar net correction and should not retry unchanged references.
    right = CoordinationNetworkCalibration(
        (0.1,), (0.31,), (FixedShuntReference("C", 0.2, (0.21,)),)
    )
    assert equivalent_coordination_calibration(left, right, policy)
    changed_p = CoordinationNetworkCalibration((0.10001,), (0.31,), right.fixed_shunts)
    assert not equivalent_coordination_calibration(left, changed_p, policy)
    changed_q = CoordinationNetworkCalibration((0.1,), (0.311,), right.fixed_shunts)
    assert not equivalent_coordination_calibration(left, changed_q, policy)
    wrong_mode = CoordinationNetworkCalibration(
        (0.1,), (0.3,), (FixedShuntReference("C", -0.2, (-0.2,)),)
    )
    with pytest.raises(ValueError, match="operating modes"):
        equivalent_coordination_calibration(left, wrong_mode, policy)

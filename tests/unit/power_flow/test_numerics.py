"""Original-equation residuals, singularity guard and independent input validity."""

import numpy as np
import pytest

from oilfield_energy.modules.power_flow.api import (
    assess_power_balance,
    constant_power_current,
    validate_bus_demands,
)
from oilfield_energy.modules.power_flow.contracts import (
    FlowNumerics,
    RadialBalanceBranch,
    SingularPowerVoltage,
)


@pytest.mark.parametrize("voltage", [0.0, 1e-12, 1e-8, -1e-9])
def test_nonzero_power_has_no_synthetic_divisor(voltage):
    with pytest.raises(SingularPowerVoltage):
        constant_power_current(np.array([1 + 0j]), np.array([complex(voltage)]), 1e-8)


def test_zero_demand_and_complex_sign_are_preserved_without_mutating_inputs():
    power = np.array([0j, 1 + 2j])
    voltage = np.array([0j, 0.8 + 0.6j])
    before = power.copy(), voltage.copy()
    current = constant_power_current(power, voltage, 1e-8)
    np.testing.assert_allclose(voltage * current.conj(), power)
    assert current[0] == 0
    np.testing.assert_array_equal(power, before[0])
    np.testing.assert_array_equal(voltage, before[1])


def test_residual_does_not_cancel_errors_between_buses():
    voltage = np.ones(3, dtype=complex)
    demand = np.array([0j, 0.1 + 0.2j, -0.1 - 0.2j])
    branches = (RadialBalanceBranch(0, 1, 0.1 + 0.2j, 1), RadialBalanceBranch(0, 2, 0.1 + 0.2j, 1))
    residual = assess_power_balance(voltage, demand, np.zeros(3), branches, 0)
    assert residual.maximum_p_pu == pytest.approx(0.1)
    assert residual.maximum_q_pu == pytest.approx(0.2)
    assert sum(demand) == 0


def test_tap_and_shunt_original_equations_have_independent_analytic_equilibrium():
    v = np.array([1 + 0j, 1 / 1.02 + 0j])
    shunt = np.array([0.0, 0.02])
    demand = np.array([0j, 1j * shunt[1] * abs(v[1]) ** 2])
    result = assess_power_balance(v, demand, shunt, (RadialBalanceBranch(0, 1, 0.1j, 1.02),), 0)
    assert result.maximum_pu < 1e-14


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), True])
@pytest.mark.parametrize(
    "field", ["voltage_step_tolerance_pu", "power_balance_tolerance_pu", "singular_voltage_pu"]
)
def test_each_numerical_tolerance_is_explicit_and_validated(field, value):
    with pytest.raises(ValueError):
        FlowNumerics(**{field: value})


@pytest.mark.parametrize(
    "value", [None, True, np.bool_(True), "1", 1j, float("nan"), float("inf"), 10**400]
)
def test_invalid_numeric_inputs_form_reasons_without_crashing(value):
    reasons = validate_bus_demands(
        ("A",), {"A": value}, {"A": 0.0}, quality_valid=True, coherent=True
    )
    assert any(r.startswith("P_") for r in reasons)


def test_numpy_reals_and_explicit_quality_are_supported():
    assert not validate_bus_demands(
        ("A",), {"A": np.float32(1)}, {"A": np.float64(0)}, quality_valid=True, coherent=True
    )
    assert validate_bus_demands(
        ("A",), {"A": 1}, {"A": 0}, quality_valid=False, coherent=False
    ) == ("POINT_QUALITY_INVALID", "POINT_INCOHERENT")

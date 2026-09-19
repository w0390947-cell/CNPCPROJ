from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.ac_power_flow import validate_ac_dispatch
from oilfield_energy.data import build_synthetic_case
from oilfield_energy.model import solve_case
from oilfield_energy.resource_control_contracts import ResourceSchedule, ResourceType
from oilfield_energy.network_model import (
    NetworkBranchKind,
    NetworkOperatingMode,
    ShuntCompensator,
    ShuntKind,
    ShuntOperatingPoint,
)


class ExactBranchFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = build_synthetic_case(steps=8)
        cls.names = [mg.name for mg in cls.case.microgrids]
        cls.legacy = solve_case(
            cls.case,
            cls.names,
            storage_enabled=True,
            cluster_coordination=True,
            time_limit_seconds=60,
        )
        cls.checked = solve_case_ac_consistent(
            cls.case,
            cls.names,
            time_limit_seconds=60,
        )

    def test_misocp_cone_and_ac_losses_are_consistent(self) -> None:
        self.assertTrue(self.checked.passed, self.checked.stop_reason)
        result = self.checked.optimization
        final = self.checked.history[-1]
        self.assertLess(final.cone_relative_gap, 1e-6)
        self.assertLess(final.line_p_loss_difference_mw, 1e-6)
        self.assertLess(final.line_q_loss_difference_mvar, 1e-6)
        self.assertLess(final.voltage_difference_pu, 1e-6)
        self.assertGreater(float(result.cluster["total_active_loss_mwh"]), 0.0)
        self.assertGreater(float(result.cluster["total_reactive_loss_mvarh"]), 0.0)

    def test_relaxed_storage_solution_certifies_original_misocp(self) -> None:
        result = self.checked.optimization
        self.assertTrue(result.cluster["storage_binary_relaxation_used"])
        self.assertTrue(result.cluster["storage_integrality_certified"])
        self.assertLessEqual(float(result.cluster["maximum_simultaneous_storage_mw"]), 1e-7)
        for name in self.names:
            modes = np.asarray(result.microgrids[name]["storage_charge_mode"])
            self.assertTrue(np.all(np.isin(modes, [0.0, 1.0])))

    def test_exact_model_removes_legacy_ac_loss_mismatch(self) -> None:
        self.assertTrue(self.legacy.success, self.legacy.message)
        legacy_ac = validate_ac_dispatch(self.case, self.legacy, self.names)
        legacy_model_loss = float(sum(
            np.sum(np.asarray(self.legacy.microgrids[name]["loss_mw"]))
            * self.case.assumptions.dt_hours
            for name in self.names
        ))
        legacy_ac_loss = float(sum(
            item["total_loss_mwh"] for item in legacy_ac["microgrids"].values()
        ))
        legacy_error = abs(legacy_model_loss - legacy_ac_loss)
        exact_error = self.checked.history[-1].active_loss_difference_mwh
        self.assertGreater(legacy_error, 1e-3)
        self.assertLess(exact_error, 1e-6)
        self.assertLess(exact_error, legacy_error)

    def test_exact_result_preserves_per_resource_pq_schedules(self) -> None:
        result = self.checked.optimization
        self.assertTrue(result.success, result.message)
        for microgrid in self.case.microgrids:
            values = result.microgrids[microgrid.name]
            schedules = values["resource_schedules"]
            self.assertIsInstance(schedules, tuple)
            self.assertEqual(
                len(schedules),
                len(microgrid.wind_available_mw)
                + len(microgrid.pv_available_mw)
                + 2,
            )
            self.assertTrue(all(isinstance(item, ResourceSchedule) for item in schedules))
            self.assertEqual(
                len({item.resource_id for item in schedules}),
                len(schedules),
            )

            wind_p = values["wind_active_by_bus_mw"]
            wind_q = values["wind_reactive_by_bus_mvar"]
            pv_p = values["pv_active_by_bus_mw"]
            pv_q = values["pv_reactive_by_bus_mvar"]
            np.testing.assert_allclose(
                np.sum(np.vstack(tuple(wind_p.values())), axis=0),
                values["wind_used_mw"],
            )
            np.testing.assert_allclose(
                np.sum(np.vstack(tuple(wind_q.values())), axis=0),
                values["wind_q_mvar"],
            )
            np.testing.assert_allclose(
                np.sum(np.vstack(tuple(pv_p.values())), axis=0),
                values["pv_used_mw"],
            )
            np.testing.assert_allclose(
                np.sum(np.vstack(tuple(pv_q.values())), axis=0),
                values["pv_q_mvar"],
            )
            np.testing.assert_allclose(
                values["storage_active_mw"],
                np.asarray(values["storage_discharge_mw"])
                - np.asarray(values["storage_charge_mw"]),
            )
            self.assertEqual(
                {item.resource_type for item in schedules},
                {
                    ResourceType.WIND,
                    ResourceType.PV,
                    ResourceType.STORAGE,
                    ResourceType.SVG,
                },
            )

    def test_exact_model_respects_field_reactive_capability_contract(self) -> None:
        original = self.case.microgrids[0]
        wind_bus = next(iter(original.wind_available_mw))
        pv_bus = next(iter(original.pv_available_mw))
        field_microgrid = replace(
            original,
            wind_q_abs_over_p_max={wind_bus: 0.30},
            pv_reactive_enabled={pv_bus: False},
            storage_reactive_enabled=False,
        )
        field_case = replace(self.case, microgrids=[field_microgrid])
        checked = solve_case_ac_consistent(
            field_case, [field_microgrid.name], time_limit_seconds=60,
        )
        self.assertTrue(checked.passed, checked.stop_reason)
        values = checked.optimization.microgrids[field_microgrid.name]
        for bus in field_microgrid.wind_available_mw:
            p_wind = np.asarray(values["wind_active_by_bus_mw"][bus])
            q_wind = np.asarray(values["wind_reactive_by_bus_mvar"][bus])
            self.assertTrue(np.all(np.abs(q_wind) <= 0.30 * p_wind + 1e-7))
        self.assertTrue(np.allclose(values["pv_q_mvar"], 0.0, atol=1e-8))
        self.assertTrue(np.allclose(values["storage_q_mvar"], 0.0, atol=1e-8))

    def test_fixed_transformer_tap_and_shunt_match_independent_ac(self) -> None:
        original = self.case.microgrids[0]
        original_network = original.network_model_v2
        self.assertIsNotNone(original_network)
        transformer = replace(
            original_network.branches[0],  # type: ignore[union-attr]
            kind=NetworkBranchKind.TRANSFORMER,
            fixed_tap_ratio=1.01,
            provenance="synthetic transformer regression",
        )
        shunt = ShuntCompensator(
            shunt_id="SC_FIXED_CAP",
            bus_id=original.svg_bus,
            kind=ShuntKind.CAPACITOR,
            q_per_step_mvar=0.10,
            minimum_steps=0,
            maximum_steps=4,
            default_steps=0,
            provenance="synthetic shunt regression",
        )
        mode = NetworkOperatingMode(
            mode_id="tap-and-shunt",
            description="fixed off-nominal tap and two capacitor steps",
            shunt_operating_points=(
                ShuntOperatingPoint(shunt.shunt_id, True, 2),
            ),
        )
        network = replace(
            original_network,
            branches=(transformer, *original_network.branches[1:]),  # type: ignore[union-attr]
            shunts=(shunt,),
            operating_modes=(mode,),
            default_operating_mode_id=mode.mode_id,
        )
        microgrid = replace(
            original,
            network_model_v2=network,
            network_operating_mode_id=mode.mode_id,
        )
        case = replace(self.case, microgrids=[microgrid])

        checked = solve_case_ac_consistent(
            case,
            [microgrid.name],
            time_limit_seconds=60,
        )
        self.assertTrue(checked.passed, checked.stop_reason)
        values = checked.optimization.microgrids[microgrid.name]
        self.assertEqual(values["network_operating_mode_id"], mode.mode_id)
        self.assertAlmostEqual(
            values["fixed_shunt_q_nominal_mvar_by_bus"][original.svg_bus], 0.2
        )
        shunt_q = np.asarray(
            values["fixed_shunt_q_mvar_by_bus"][original.svg_bus]
        )
        bus_index = microgrid.buses.index(original.svg_bus)
        voltage = np.asarray(values["voltage_pu"])[bus_index]
        np.testing.assert_allclose(shunt_q, 0.2 * voltage ** 2, atol=1e-10)
        final = checked.history[-1]
        self.assertLess(final.cone_relative_gap, 1e-6)
        self.assertLess(final.line_p_loss_difference_mw, 1e-6)
        self.assertLess(final.line_q_loss_difference_mvar, 1e-6)
        self.assertLess(final.voltage_difference_pu, 1e-6)


if __name__ == "__main__":
    unittest.main()

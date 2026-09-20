from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.ac_power_flow import backward_forward_sweep
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.network_model import (
    BranchServiceState,
    NetworkBranchKind,
    NetworkBus,
    NetworkContingency,
    NetworkDataProvenance,
    NetworkModelError,
    NetworkModelV2,
    NetworkOperatingMode,
    NetworkPhaseModel,
    NetworkSwitch,
    SeriesBranch,
    ShuntCompensator,
    ShuntKind,
    ShuntOperatingPoint,
    SwitchState,
    TransformerTapChanger,
    TransformerTapPosition,
    assess_n_minus_one,
    assess_network_model,
    validate_legacy_network_alignment,
)


def _provenance() -> NetworkDataProvenance:
    return NetworkDataProvenance(
        source="approved-network-register.json",
        dataset_version="2026-09-07.1",
        approved_for_field_use=True,
        approved_by="project-owner",
        approved_at_utc=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )


def _three_bus_network(
    *,
    branches: tuple[SeriesBranch, ...] | None = None,
    switches: tuple[NetworkSwitch, ...] = (),
    modes: tuple[NetworkOperatingMode, ...] | None = None,
    shunts: tuple[ShuntCompensator, ...] = (),
    contingencies: tuple[NetworkContingency, ...] = (),
) -> NetworkModelV2:
    default_branches = (
        SeriesBranch(
            "L01", "PCC", "B1", NetworkBranchKind.LINE,
            0.01, 0.02, 10.0, provenance="approved line register",
        ),
        SeriesBranch(
            "L12", "B1", "B2", NetworkBranchKind.LINE,
            0.02, 0.03, 8.0, provenance="approved line register",
        ),
    )
    default_modes = (NetworkOperatingMode("normal", "normal operation"),)
    return NetworkModelV2(
        network_id="field-test",
        base_mva=10.0,
        pcc_bus_id="PCC",
        buses=(
            NetworkBus("PCC", "PCC", 35.0, "single line diagram"),
            NetworkBus("B1", "Bus 1", 10.0, "single line diagram"),
            NetworkBus("B2", "Bus 2", 10.0, "single line diagram"),
        ),
        branches=default_branches if branches is None else branches,
        switches=switches,
        shunts=shunts,
        operating_modes=default_modes if modes is None else modes,
        default_operating_mode_id=(default_modes if modes is None else modes)[0].mode_id,
        contingencies=contingencies,
        provenance=_provenance(),
    )


class NetworkModelV2Tests(unittest.TestCase):
    def test_synthetic_case_is_attached_to_and_aligned_with_v2(self) -> None:
        case = build_synthetic_case(steps=4)
        for microgrid in case.microgrids:
            self.assertIsNotNone(microgrid.network_model_v2)
            resolved = validate_legacy_network_alignment(microgrid)
            self.assertEqual(resolved.pcc_bus_id, microgrid.pcc_bus)
            self.assertEqual(
                tuple(branch.branch_id for branch in resolved.branches),
                tuple(line.name for line in microgrid.lines),
            )
            self.assertTrue(
                microgrid.network_model_v2.provenance.synthetic  # type: ignore[union-attr]
            )

    def test_legacy_view_cannot_diverge_from_v2_silently(self) -> None:
        microgrid = build_synthetic_case(steps=4).microgrids[0]
        original = microgrid.lines[0]
        divergent = replace(
            microgrid,
            lines=[replace(original, r_pu=original.r_pu * 2.0), *microgrid.lines[1:]],
        )
        with self.assertRaisesRegex(NetworkModelError, "diverges"):
            validate_legacy_network_alignment(divergent)

    def test_operating_mode_resolves_switches_before_radial_validation(self) -> None:
        branches = (
            SeriesBranch(
                "L01", "PCC", "B1", NetworkBranchKind.LINE,
                0.01, 0.02, 10.0, provenance="register",
            ),
            SeriesBranch(
                "L12", "B1", "B2", NetworkBranchKind.LINE,
                0.02, 0.03, 8.0, provenance="register",
            ),
            SeriesBranch(
                "L02", "PCC", "B2", NetworkBranchKind.LINE,
                0.03, 0.04, 8.0, provenance="register",
            ),
        )
        switches = (NetworkSwitch("SW02", "L02", normally_closed=False),)
        modes = (
            NetworkOperatingMode("normal", "L12 supplies B2"),
            NetworkOperatingMode(
                "alternate",
                "L02 supplies B2",
                switch_states=(SwitchState("SW02", True),),
                branch_states=(BranchServiceState("L12", False),),
            ),
        )
        model = _three_bus_network(
            branches=branches, switches=switches, modes=modes
        )
        normal = assess_network_model(model, "normal")
        alternate = assess_network_model(model, "alternate")
        self.assertTrue(normal.ready_for_field_case, normal.blockers)
        self.assertTrue(alternate.ready_for_field_case, alternate.blockers)
        self.assertEqual(
            {item.branch_id for item in normal.resolved_network.branches},  # type: ignore[union-attr]
            {"L01", "L12"},
        )
        self.assertEqual(
            {item.branch_id for item in alternate.resolved_network.branches},  # type: ignore[union-attr]
            {"L01", "L02"},
        )

    def test_transformer_tap_is_resolved_for_current_solver(self) -> None:
        transformer = SeriesBranch(
            "T01", "PCC", "B1", NetworkBranchKind.TRANSFORMER,
            0.01, 0.04, 12.0,
            tap_changer=TransformerTapChanger(-2, 2, 0, 0, 1.25),
            provenance="transformer test report",
        )
        line = SeriesBranch(
            "L12", "B1", "B2", NetworkBranchKind.LINE,
            0.02, 0.03, 8.0, provenance="line register",
        )
        mode = NetworkOperatingMode(
            "tap-up",
            "transformer tap raised",
            transformer_taps=(TransformerTapPosition("T01", 1),),
        )
        model = _three_bus_network(branches=(transformer, line), modes=(mode,))
        readiness = assess_network_model(model, "tap-up")
        self.assertTrue(readiness.structurally_valid)
        self.assertTrue(readiness.radial_connected)
        self.assertTrue(readiness.current_solver_compatible, readiness.blockers)
        self.assertAlmostEqual(
            readiness.resolved_network.branches[0].tap_ratio, 1.0125  # type: ignore[union-attr]
        )

    def test_shunt_steps_are_resolved_for_current_solver(self) -> None:
        shunt = ShuntCompensator(
            "C1", "B2", ShuntKind.CAPACITOR,
            q_per_step_mvar=0.2,
            minimum_steps=0,
            maximum_steps=4,
            default_steps=0,
            provenance="capacitor register",
        )
        mode = NetworkOperatingMode(
            "capacitor-on",
            "two capacitor steps",
            shunt_operating_points=(ShuntOperatingPoint("C1", True, 2),),
        )
        readiness = assess_network_model(
            _three_bus_network(shunts=(shunt,), modes=(mode,)), "capacitor-on"
        )
        self.assertTrue(readiness.current_solver_compatible, readiness.blockers)
        self.assertAlmostEqual(
            readiness.resolved_network.shunt_q_mvar_by_bus["B2"], 0.4  # type: ignore[union-attr]
        )

    def test_reversed_transformer_orientation_inverts_tap_ratio(self) -> None:
        transformer = SeriesBranch(
            "T01", "B1", "PCC", NetworkBranchKind.TRANSFORMER,
            0.01, 0.04, 12.0,
            fixed_tap_ratio=1.02,
            provenance="transformer test report",
        )
        line = SeriesBranch(
            "L12", "B1", "B2", NetworkBranchKind.LINE,
            0.02, 0.03, 8.0, provenance="line register",
        )
        readiness = assess_network_model(
            _three_bus_network(branches=(transformer, line))
        )
        self.assertTrue(readiness.current_solver_compatible, readiness.blockers)
        resolved = readiness.resolved_network
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.branches[0].parent, "PCC")  # type: ignore[union-attr]
        self.assertAlmostEqual(
            resolved.branches[0].tap_ratio, 1.0 / 1.02  # type: ignore[union-attr]
        )

    def test_n_minus_one_is_evaluated_and_islanding_fails_closed(self) -> None:
        contingency = NetworkContingency("N-1-L12", ("L12",))
        model = _three_bus_network(contingencies=(contingency,))
        reports = assess_n_minus_one(model)
        self.assertEqual(set(reports), {"N-1-L12"})
        self.assertFalse(reports["N-1-L12"].radial_connected)
        self.assertIn("TOPOLOGY_ISLANDED", reports["N-1-L12"].blockers)

    def test_ac_no_load_voltage_follows_fixed_transformer_ratio(self) -> None:
        original = build_synthetic_case(steps=1).microgrids[0]
        network = original.network_model_v2
        self.assertIsNotNone(network)
        transformer = replace(
            network.branches[0],  # type: ignore[union-attr]
            kind=NetworkBranchKind.TRANSFORMER,
            fixed_tap_ratio=1.02,
        )
        modified_network = replace(
            network,
            branches=(transformer, *network.branches[1:]),  # type: ignore[union-attr]
        )
        microgrid = replace(original, network_model_v2=modified_network)
        flow = backward_forward_sweep(
            microgrid,
            np.zeros(len(microgrid.buses)),
            np.zeros(len(microgrid.buses)),
        )
        self.assertTrue(flow["converged"])
        voltage = np.asarray(flow["voltage_pu"])
        self.assertAlmostEqual(voltage[0], 1.0)
        np.testing.assert_allclose(voltage[1:], 1.0 / 1.02, atol=1e-10)

    def test_ac_fixed_capacitor_enters_q_balance_with_capacitive_sign(self) -> None:
        original = build_synthetic_case(steps=1).microgrids[0]
        network = original.network_model_v2
        self.assertIsNotNone(network)
        shunt = ShuntCompensator(
            "C_FIXED", original.svg_bus, ShuntKind.CAPACITOR,
            0.2, 0, 1, 1, provenance="synthetic shunt regression",
        )
        modified_network = replace(network, shunts=(shunt,))
        microgrid = replace(original, network_model_v2=modified_network)
        flow = backward_forward_sweep(
            microgrid,
            np.zeros(len(microgrid.buses)),
            np.zeros(len(microgrid.buses)),
        )
        self.assertTrue(flow["converged"])
        voltage = np.asarray(flow["voltage_pu"])
        shunt_voltage = voltage[microgrid.buses.index(original.svg_bus)]
        actual_capacitive_q = 0.2 * shunt_voltage ** 2
        self.assertAlmostEqual(
            float(flow["pcc_q_mvar"]),
            -actual_capacitive_q + float(flow["reactive_loss_mvar"]),
            places=9,
        )

    def test_duplicate_ids_and_unknown_references_are_reported(self) -> None:
        model = _three_bus_network()
        invalid = replace(
            model,
            buses=(model.buses[0], model.buses[0], model.buses[2]),
        )
        readiness = assess_network_model(invalid)
        self.assertFalse(readiness.structurally_valid)
        self.assertIn("DUPLICATE_BUS_ID", readiness.blockers)

    def test_phase_shift_and_three_phase_models_remain_fail_closed(self) -> None:
        model = _three_bus_network()
        phase_shifting = replace(
            model.branches[0],
            kind=NetworkBranchKind.TRANSFORMER,
            phase_shift_degrees=5.0,
        )
        phase_shift_model = replace(
            model,
            branches=(phase_shifting, model.branches[1]),
        )
        phase_shift_readiness = assess_network_model(phase_shift_model)
        self.assertFalse(phase_shift_readiness.current_solver_compatible)
        self.assertIn("PHASE_SHIFT_NOT_IMPLEMENTED", phase_shift_readiness.blockers)

        unbalanced = replace(
            model,
            phase_model=NetworkPhaseModel.THREE_PHASE_UNBALANCED,
        )
        unbalanced_readiness = assess_network_model(unbalanced)
        self.assertFalse(unbalanced_readiness.current_solver_compatible)
        self.assertIn(
            "THREE_PHASE_MODEL_NOT_IMPLEMENTED",
            unbalanced_readiness.blockers,
        )


if __name__ == "__main__":
    unittest.main()

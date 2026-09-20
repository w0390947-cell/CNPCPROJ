from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oilfield_energy.field_data import (
    ActivePowerUnit,
    D5000ActivePointDefinition,
    D5000Measurement,
    D5000PccAdapter,
    D5000Quality,
    FieldCaseBuilder,
    FieldDataNotReadyError,
    MeasurementDirection,
    audit_line_load_workbook,
    build_project_asset_registry,
    import_short_circuit_workbook,
)
from tests.legacy_case_fixture import build_synthetic_case


class FieldDataContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        # Parser contracts use committed synthetic fixtures, never private client files.
        fixtures = root / "tests/fixtures/audit"
        cls.short_circuit = import_short_circuit_workbook(fixtures / "short-circuit-synthetic.xlsx")
        cls.load_history = audit_line_load_workbook(fixtures / "line-load-invalid-synthetic.xlsx")
        cls.registry = build_project_asset_registry()

    def test_short_circuit_workbook_is_imported_without_inventing_units(self) -> None:
        workbook = self.short_circuit
        self.assertEqual(workbook.sheet_name, "合成母线参数")
        self.assertEqual(len(workbook.records), 3)
        first = workbook.records[0]
        self.assertEqual(first.station_name, "模拟站-SC")
        self.assertEqual(first.bus_name, "35kV主母线")
        self.assertAlmostEqual(first.positive_sequence_impedance_max_ohm, 3.1)
        self.assertAlmostEqual(first.zero_sequence_impedance_max_ohm, 6.2)
        self.assertEqual(
            workbook.quality.issue_counts["ENGINEERING_UNIT_MISSING"], 6
        )
        self.assertFalse(workbook.quality.valid)

    def test_load_history_audit_exposes_corruption_and_performs_no_conversion(self) -> None:
        workbook = self.load_history
        self.assertEqual(len(workbook.sheets), 1)
        self.assertFalse(workbook.raw_units_confirmed)
        self.assertFalse(workbook.raw_direction_confirmed)
        self.assertFalse(workbook.metadata["conversion_performed"])
        corrupt = next(item for item in workbook.sheets if item.name == "合成异常线路记录")
        self.assertEqual(corrupt.invalid_timestamp_count, 1)
        self.assertGreater(workbook.quality.issue_counts["MEASUREMENT_NONNUMERIC"], 0)
        self.assertGreater(workbook.quality.issue_counts["POWER_FACTOR_OUT_OF_RANGE"], 0)
        self.assertGreater(workbook.quality.issue_counts["NONPOSITIVE_VOLTAGE"], 0)
        self.assertGreater(workbook.quality.issue_counts["ENGINEERING_UNIT_MISSING"], 0)

    def test_project_registry_contains_only_traceable_capacity_and_capability(self) -> None:
        registry = self.registry
        self.assertEqual(registry.asset("wind-xinghe").active_capacity_mw, 30.0)
        self.assertEqual(registry.asset("wind-shancheng").active_capacity_mw, 10.0)
        self.assertAlmostEqual(
            registry.asset("wind-shancheng").capability.q_abs_over_p_max, 0.30
        )
        self.assertFalse(registry.pv_default_capability.reactive_controllable)
        self.assertFalse(
            registry.asset("storage-shancheng").capability.reactive_controllable
        )

    def test_pcc_direction_is_normalized_at_the_adapter_boundary(self) -> None:
        direction = MeasurementDirection.BUS_TO_LINE_POSITIVE
        self.assertEqual(direction.raw_to_internal_import(-3.2), 3.2)
        self.assertEqual(direction.raw_to_internal_import(1.4), -1.4)

    def test_d5000_adapter_requires_explicit_point_unit_scale_and_freshness(self) -> None:
        now = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
        adapter = D5000PccAdapter(
            D5000ActivePointDefinition(
                point_id="PCC_RAW_P",
                unit=ActivePowerUnit.KW,
                engineering_multiplier=1.0,
                direction=MeasurementDirection.BUS_TO_LINE_POSITIVE,
            ),
            stale_after_seconds=5.0,
            future_clock_skew_seconds=1.0,
        )
        valid = adapter.normalize(
            D5000Measurement(
                "PCC_RAW_P", -47_511.0, now, D5000Quality.GOOD
            ),
            decision_at_utc=now,
        )
        self.assertTrue(valid.valid)
        self.assertAlmostEqual(valid.p_grid_import_mw, 47.511)

        stale = adapter.normalize(
            D5000Measurement(
                "PCC_RAW_P", -47_511.0, now - timedelta(seconds=6),
                D5000Quality.GOOD,
            ),
            decision_at_utc=now,
        )
        self.assertFalse(stale.valid)
        self.assertIsNone(stale.p_grid_import_mw)
        self.assertIn("TELEMETRY_STALE", stale.invalid_reasons)

        bad = adapter.normalize(
            D5000Measurement("PCC_RAW_P", -47_511.0, now, D5000Quality.BAD),
            decision_at_utc=now,
        )
        self.assertFalse(bad.valid)
        self.assertIn("QUALITY_NOT_GOOD", bad.invalid_reasons)

    def test_field_case_builder_fails_closed_when_source_data_is_incomplete(self) -> None:
        builder = FieldCaseBuilder(
            self.registry, self.short_circuit, self.load_history
        )
        readiness = builder.assess()
        self.assertFalse(readiness.ready)
        self.assertTrue(any("逐支路" in blocker for blocker in readiness.blockers))
        self.assertTrue(any("单位" in blocker for blocker in readiness.blockers))
        with self.assertRaises(FieldDataNotReadyError):
            builder.build()

    def test_synthetic_v2_network_cannot_satisfy_field_readiness(self) -> None:
        synthetic = build_synthetic_case(steps=4).microgrids[0].network_model_v2
        self.assertIsNotNone(synthetic)
        builder = FieldCaseBuilder(
            self.registry,
            self.short_circuit,
            self.load_history,
            network_model=synthetic,
            engineering_units_confirmed=True,
            station_mapping_confirmed=True,
            pcc_semantics_confirmed=True,
        )
        readiness = builder.assess()
        self.assertFalse(readiness.ready)
        self.assertIn(
            "网络模型阻断:SYNTHETIC_NETWORK_NOT_ALLOWED_FOR_FIELD_CASE",
            readiness.blockers,
        )
        self.assertIn(
            "网络模型阻断:NETWORK_DATASET_NOT_APPROVED",
            readiness.blockers,
        )


if __name__ == "__main__":
    unittest.main()

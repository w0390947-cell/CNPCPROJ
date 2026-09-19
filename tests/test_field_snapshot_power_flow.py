from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oilfield_energy.field_data.case_builder import FieldCaseBuilder
from oilfield_energy.field_data.snapshot_power_flow import (
    build_field_snapshot, calculate_field_power_flow, read_snapshot_request,
    run_field_power_flow_file,
)
from oilfield_energy.power_flow_comparison import read_comparison_request, compare_fixed_states

EXAMPLE = ROOT / "docs/examples/field_snapshot_request.json"


class FieldSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.request = read_snapshot_request(EXAMPLE)
        self.csv_path = self.base / self.request.sources[0].path
        self.csv_path.write_bytes((EXAMPLE.parent / self.request.sources[0].path).read_bytes())

    def edit_rows(self, change):
        with self.csv_path.open(encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        change(rows)
        with self.csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["point", "time", "value", "quality"])
            writer.writeheader()
            writer.writerows(rows)

    def result(self, request=None, demo=True):
        return calculate_field_power_flow(request or self.request, self.base, demo=demo)

    def assert_invalid(self, code, request=None, demo=True):
        report = self.result(request, demo)
        self.assertEqual(report["status"], "invalid_input", report)
        self.assertIn(code, ";".join(report["reasons"]))
        self.assertIsNone(report["flow"])
        return report

    def test_fixed_inputs_conversions_and_conservation_without_optimization(self):
        original = asdict(self.request)
        with patch("oilfield_energy.model.solve_case", side_effect=AssertionError("optimizer")), \
             patch("oilfield_energy.ac_consistency.solve_case_ac_consistent", side_effect=AssertionError("redispatch")), \
             patch("oilfield_energy.data.build_synthetic_case", side_effect=AssertionError("fallback")):
            report = self.result()
        self.assertEqual(report["status"], "secure", report)
        self.assertEqual(original, asdict(self.request))
        snapshot = FieldCaseBuilder.build_fixed_snapshot(self.request, self.base, demo=True)
        devices = {d.resource_id: d for d in snapshot.devices}
        self.assertEqual(len(devices), 5)
        self.assertEqual(devices["WT1"].p_mw, 1.0)
        self.assertEqual(devices["WT2"].p_mw, 1.0)
        self.assertEqual(devices["ESS1"].p_mw, -0.2)
        self.assertAlmostEqual(snapshot.slack_voltage_pu, 1.01)
        expected = compare_fixed_states(read_comparison_request(EXAMPLE.parent / "fixed_device_comparison.json"))["before"]
        for key in ("pcc_import_mw", "pcc_reactive_mvar", "loss_mw", "power_factor"):
            self.assertAlmostEqual(report["flow"][key], expected[key])
        self.assertAlmostEqual(report["flow"]["pcc_import_mw"],
                               sum(report["operating_point"]["p_demand_mw_by_bus"].values()) + report["flow"]["loss_mw"], places=8)
        self.assertEqual(report["flow"]["branches"][0]["kind"], "transformer")
        self.assertGreater(report["flow"]["branches"][0]["sending_current_a"], 0)
        self.assertFalse(report["redispatch_performed"])
        self.assertFalse(report["field_acceptance_certified"])

    def test_demo_is_explicit_and_field_model_must_be_approved(self):
        self.assert_invalid("SYNTHETIC_INPUT_REQUIRES_EXPLICIT_DEMO", demo=False)
        model = replace(self.request.network, provenance=replace(self.request.network.provenance, synthetic=False))
        req = replace(self.request, synthetic=False, network=model)
        self.assert_invalid("NETWORK_DATASET_NOT_APPROVED", req, demo=False)
        self.assert_invalid("DEMO_REQUIRES_SYNTHETIC_INPUT", req)

    def test_field_gate_accepts_complete_approved_fixture_without_claiming_field_validation(self):
        # This deliberately fabricated approval exercises the branch, not field acceptance.
        net = self.request.network
        net = replace(net, branches=tuple(replace(b, provenance="test fixture") for b in net.branches),
                      provenance=replace(net.provenance, synthetic=False, approved_for_field_use=True,
                                         approved_by="test fixture only", approved_at_utc=self.request.at))
        report = self.result(replace(self.request, synthetic=False, network=net), demo=False)
        self.assertEqual(report["status"], "secure", report)
        self.assertFalse(report["field_acceptance_certified"])

    def test_missing_and_bad_latest_sample_do_not_use_zero_or_older_good_value(self):
        self.edit_rows(lambda rows: rows.pop(0))
        self.assert_invalid("MISSING_CAUSAL_MEASUREMENT:WT1.p")
        self.setUp()
        def bad_latest(rows):
            old = dict(rows[0], time=(self.request.at - timedelta(seconds=1)).isoformat())
            rows.append(old)
            rows[0]["quality"] = "BAD"
        self.edit_rows(bad_latest)
        report = self.assert_invalid("QUALITY_NOT_GOOD:WT1.p")
        self.assertEqual(report["selected_measurements"][0]["quality"], "BAD")

    def test_time_freshness_future_and_coherence(self):
        for delta, code in ((-61, "STALE_MEASUREMENT"), (1, "MISSING_CAUSAL_MEASUREMENT"),
                            (-6, "SNAPSHOT_NOT_COHERENT")):
            with self.subTest(delta=delta):
                self.edit_rows(lambda rows: rows[0].update(time=(self.request.at + timedelta(seconds=delta)).isoformat()))
                self.assert_invalid(code)

    def test_recent_causal_sample_selected_and_future_sample_ignored(self):
        def edit(rows):
            rows.append(dict(rows[0], time=(self.request.at + timedelta(seconds=1)).isoformat(), value="9999"))
            rows[0]["time"] = (self.request.at - timedelta(seconds=2)).isoformat()
        self.edit_rows(edit)
        self.assertEqual(self.result()["snapshot"]["devices"][0]["p_mw"], 1)

    def test_duplicate_timestamp_is_rejected(self):
        self.edit_rows(lambda rows: rows.append(dict(rows[0])))
        self.assert_invalid("DUPLICATE_POINT_TIMESTAMP")

    def test_invalid_value_status_and_out_of_service_nonzero(self):
        for value in ("", "NaN", "inf", "not-a-number"):
            with self.subTest(value=value):
                self.edit_rows(lambda rows: rows[0].update(value=value))
                self.assert_invalid("MEASUREMENT_NON")
        self.edit_rows(lambda rows: rows[0].update(value="100"))
        self.edit_rows(lambda rows: rows[2].update(value="2"))
        self.assert_invalid("INVALID_DEVICE_STATUS")
        self.edit_rows(lambda rows: rows[2].update(value="0"))
        self.assert_invalid("out-of-service devices")

    def test_mapping_coverage_scopes_and_zero_load_are_not_guessed(self):
        r = self.request
        cases = [
            (replace(r, points=r.points[:-1]), "POINT_COVERAGE_MISMATCH"),
            (replace(r, points=r.points + (r.points[0],)), "DUPLICATE_TARGET_QUANTITY"),
            (replace(r, zero_load_bus_ids=()), "LOAD_BUS_COVERAGE_INCOMPLETE"),
            (replace(r, zero_load_bus_ids=("PCC", "MAIN")), "ZERO_AND_MEASURED_LOAD_OVERLAP"),
            (replace(r, devices=(replace(r.devices[0], bus_id="unknown"),) + r.devices[1:]), "DEVICE_BUS_UNKNOWN"),
            (replace(r, loads=(r.loads[0], replace(r.loads[1], scope_members=r.loads[0].scope_members))), "DUPLICATE:load_scope_member"),
            (replace(r, loads=(replace(r.loads[0], basis="net_feeder"),) + r.loads[1:]), "LOAD_BUS_OR_BASIS_INVALID"),
            (replace(r, inventory_complete=False), "UNCONFIRMED"),
        ]
        for req, code in cases:
            with self.subTest(code=code):
                self.assert_invalid(code, req)

    def test_multiple_disjoint_loads_on_one_bus_are_summed(self):
        r = self.request
        extra = replace(r.loads[0], load_id="SECOND", scope_members=("second_consumer",))
        points = tuple(replace(p, point_id="SECOND." + p.quantity, target_id="SECOND")
                       for p in r.points if p.target_id == r.loads[0].load_id)
        self.edit_rows(lambda rows: rows.extend([
            dict(point="SECOND.p", time=r.at.isoformat(), value="500", quality="GOOD"),
            dict(point="SECOND.q", time=r.at.isoformat(), value="100", quality="GOOD")]))
        report = self.result(replace(r, loads=r.loads + (extra,), points=r.points + points))
        self.assertAlmostEqual(report["operating_point"]["p_demand_mw_by_bus"]["MAIN"], 5.2)

    def test_units_multiplier_and_timestamp_mode_are_checked(self):
        r = self.request
        for point in (replace(r.points[0], unit="kV"), replace(r.points[0], multiplier=0),
                      replace(r.points[0], positive_direction="consumption")):
            self.assert_invalid("INVALID", replace(r, points=(point,) + r.points[1:]))
        self.assert_invalid("OPERATING_MODE_NOT_VALID", replace(r, at=r.operating_mode_valid_until))
        self.assert_invalid("TIMEZONE_REQUIRED", replace(r, at=r.at.replace(tzinfo=None)))

    def test_naive_timestamp_uses_declared_offset(self):
        self.edit_rows(lambda rows: [row.update(time="2026/09/13 12:00:00") for row in rows])
        req = replace(self.request, sources=(replace(self.request.sources[0], timestamp_format="%Y/%m/%d %H:%M:%S"),))
        self.assertEqual(self.result(req)["status"], "secure")

    def test_violation_and_nonconvergence_are_preserved(self):
        r = self.request
        net = replace(r.network, branches=tuple(replace(b, s_max_mva=.1) for b in r.network.branches))
        report = self.result(replace(r, network=net))
        self.assertEqual(report["status"], "violation")
        self.assertTrue(report["flow"]["violations"])
        self.assertEqual(report["snapshot"]["devices"][0]["p_mw"], 1)
        report = self.result(replace(r, limits=replace(r.limits, max_iterations=1)))
        self.assertEqual(report["status"], "not_converged")
        self.assertIsNone(report["flow"]["pcc_import_mw"])

    def test_capability_violation_reported_without_clipping(self):
        r = self.request
        report = self.result(replace(r, devices=(replace(r.devices[0], p_max_mw=.5),) + r.devices[1:]))
        self.assertEqual(report["status"], "violation")
        self.assertEqual(report["device_violations"][0]["code"], "P_MAX")
        self.assertEqual(report["snapshot"]["devices"][0]["p_mw"], 1)

    def test_strict_manifest_rejects_extra_keys_duplicate_keys_and_average_samples(self):
        original = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        for modify in (lambda r: r["points"][0].update(sample_kind="hourly_average"),
                       lambda r: r["network"]["buses"][0].update(typo=1),
                       lambda r: r.update(synthetic="true")):
            data = json.loads(json.dumps(original))
            modify(data)
            path = self.base / "request.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_snapshot_request(path)
        path.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DUPLICATE_JSON_KEY"):
            read_snapshot_request(path)

    def test_xlsx_long_table_and_excel_timestamp(self):
        with self.csv_path.open(encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        # Generate a minimal OOXML fixture using only the standard library.
        xml_rows = []
        for i, row in enumerate(rows, 1):
            if i > 1:
                row[1] = "46278.5"  # 2026-09-13 noon, Excel 1900 epoch
            cells = ''.join(f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for j, v in enumerate(row))
            xml_rows.append(f'<row r="{i}">{cells}</row>')
        path = self.base / "measurements.xlsx"
        with ZipFile(path, "w") as z:
            z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Measurements" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(xml_rows) + '</sheetData></worksheet>')
        source = replace(self.request.sources[0], path=path.name, format="xlsx", sheet="Measurements", timestamp_format="excel1900")
        report = self.result(replace(self.request, sources=(source,)))
        self.assertEqual(report["status"], "secure", report)
        self.assertEqual(report["selected_measurements"][0]["sheet"], "Measurements")

    def test_archive_replay_and_failed_rerun_clear_success_outputs(self):
        output = self.base / "output"
        self.assertEqual(run_field_power_flow_file(EXAMPLE, output, demo=True), 0)
        before = json.loads((output / "power_flow.json").read_text(encoding="utf-8"))
        self.assertEqual(run_field_power_flow_file(output / "request.json", output, demo=True), 0)
        after = json.loads((output / "power_flow.json").read_text(encoding="utf-8"))
        self.assertEqual(before["flow"], after["flow"])
        self.assertEqual(before["input_archives"][0]["sha256"], after["input_archives"][0]["sha256"])
        self.assertEqual(run_field_power_flow_file(EXAMPLE, output, demo=False), 2)
        self.assertIsNone(json.loads((output / "snapshot.json").read_text()))
        with (output / "branches.csv").open(encoding="utf-8-sig") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

    def test_cli_target_time_and_no_demo_flag_failure(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        cmd = [sys.executable, "-m", "oilfield_energy.cli", "field-power-flow", "--input", str(EXAMPLE),
               "--output", str(self.base / "cli")]
        failure = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(failure.returncode, 2, failure.stderr)
        success = subprocess.run(cmd + ["--demo", "--at", "2026-09-13T12:00:02+08:00"], env=env, capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)


if __name__ == "__main__":
    unittest.main()

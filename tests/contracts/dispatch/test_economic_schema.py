"""New metadata does not silently recertify historical results."""

import json
from pathlib import Path

from oilfield_energy.service import SimulationResult


def test_generated_result_schema_matches_python_authority():
    root = Path(__file__).resolve().parents[3]
    stored = json.loads(
        (root / "contracts/simulation/result.schema.json").read_text(encoding="utf-8")
    )
    assert stored == SimulationResult.model_json_schema()


def test_missing_accounting_metadata_remains_unknown_without_changing_costs():
    root = Path(__file__).resolve().parents[3]
    current = json.loads(
        (root / "web/frontend/tests/fixtures/synthetic-single-result.json").read_bytes()
    )
    assert current["economic_accounting_version"] == "dispatch-economics-v2"
    old = json.loads(json.dumps(current))
    old.pop("economic_accounting_version")
    old.pop("reference_economics")
    restored = SimulationResult.model_validate(old)
    assert restored.economic_accounting_version is None
    assert restored.reference_economics is None
    assert restored.metadata.schema_version == current["metadata"]["schema_version"]
    assert restored.executive_summary.model_dump(mode="json") == old["executive_summary"]
    json.dumps(restored.model_dump(mode="json"), allow_nan=False)

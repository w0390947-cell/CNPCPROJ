"""New metadata does not silently recertify historical results."""

import json
from pathlib import Path

from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation


def test_generated_result_schema_matches_python_authority():
    root = Path(__file__).resolve().parents[3]
    stored = json.loads(
        (root / "contracts/simulation/result.schema.json").read_text(encoding="utf-8")
    )
    assert stored == SimulationResult.model_json_schema()


def test_old_snapshot_keeps_unknown_accounting_version_and_raw_costs():
    current = run_simulation(SimulationRequest(steps=4)).model_dump(mode="json")
    assert current["economic_accounting_version"] == "dispatch-economics-v2"
    old = json.loads(json.dumps(current))
    old["metadata"]["schema_version"] = "1.0.0"
    old.pop("economic_accounting_version")
    old.pop("reference_economics")
    restored = SimulationResult.model_validate(old)
    assert restored.economic_accounting_version is None
    assert restored.reference_economics is None
    assert restored.metadata.schema_version == "1.0.0"
    assert restored.executive_summary.model_dump(mode="json") == old["executive_summary"]
    json.dumps(restored.model_dump(mode="json"), allow_nan=False)

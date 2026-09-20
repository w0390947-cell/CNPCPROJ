"""Generate the Web read-only dataset facts; --check detects stale projections."""

import argparse
import json
from pathlib import Path

from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case, load_dataset


def content() -> str:
    dataset, digest = load_dataset()
    case = build_synthetic_case()
    facts = {
        "schema_version": "dataset-facts-v1",
        "dataset_id": dataset.dataset_id,
        "revision": dataset.revision,
        "sha256": digest,
        "clusterImportLimitMw": case.cluster_import_limit_mw,
        "regions": {
            mg.name: {
                "loadScaleMw": round(mg.maximum_load_mw, 8),
                "windMw": sum(mg.wind_capacity_mw.values()),
                "pvMw": sum(mg.pv_capacity_mw.values()),
                "storage": f"{mg.storage.p_max_mw:g} MW / {mg.storage.e_max_mwh:g} MWh",
                "busCount": len(mg.buses),
                "branchCount": len(mg.lines),
                "voltageMinPu": mg.voltage_min_pu,
                "voltageMaxPu": mg.voltage_max_pu,
                "pccMinMw": mg.p_grid_min_mw,
            }
            for mg in case.microgrids
        },
    }
    return json.dumps(facts, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = (
        Path(__file__).resolve().parents[1] / "web/frontend/shared/api/generated/dataset-facts.json"
    )
    expected = content()
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            raise SystemExit("dataset facts are stale; run tools/export_unified_dataset.py")
    else:
        target.write_text(expected, encoding="utf-8")


if __name__ == "__main__":
    main()

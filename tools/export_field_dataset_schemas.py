"""Export the installed package's authoritative dataset boundary schemas."""

import argparse
import json
from pathlib import Path

from oilfield_energy.modules.measurements.contracts import DatasetManifest
from oilfield_energy.workflows.field_dataset.adapters.document_assembly import DocumentEnvelope


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, model in (("manifest", DatasetManifest), ("document", DocumentEnvelope)):
        (args.output / f"{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

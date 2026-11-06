"""Copy a schema 1.26.0 non-group result to schema 2.0.0 without recomputation.

The original is never overwritten. An adjacent migration receipt records its hash.
Retired standalone group results remain historical artifacts and are rejected.
"""

import argparse
import hashlib
import json
from pathlib import Path

from oilfield_energy.service import SCHEMA_VERSION, SimulationResult


def migrate_result(payload: dict) -> dict:
    """Remove only the retired null field and update the declared schema version."""
    if payload.get("metadata", {}).get("schema_version") != "1.26.0":
        raise ValueError("migration requires schema 1.26.0")
    if (
        payload["metadata"].get("scenario_type") == "group_control"
        or payload.get("group_control") is not None
    ):
        raise ValueError(
            "standalone group-control results must remain historical artifacts"
        )
    migrated = dict(payload)
    migrated.pop("group_control", None)
    migrated["metadata"] = dict(payload["metadata"], schema_version=SCHEMA_VERSION)
    SimulationResult.model_validate(migrated)
    return migrated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    receipt = args.output.with_suffix(args.output.suffix + ".migration.json")
    if (
        args.source.resolve() == args.output.resolve()
        or args.output.exists()
        or receipt.exists()
    ):
        parser.error("output and receipt must be new files, distinct from the source")
    original = args.source.read_bytes()
    migrated = migrate_result(json.loads(original))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    receipt.write_text(
        json.dumps(
            {
                "source_schema": "1.26.0",
                "target_schema": SCHEMA_VERSION,
                "source_sha256": hashlib.sha256(original).hexdigest(),
                "recomputed": False,
                "changes": [
                    "remove null group_control",
                    "update metadata.schema_version",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

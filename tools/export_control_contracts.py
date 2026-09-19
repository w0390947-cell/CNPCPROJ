"""Generate the minute-execution sidecar schema from its Python contract."""

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from oilfield_energy.modules.control.contracts import (
    MinuteExecutionRecord,
    StorageDynamicsTrajectory,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schemas = {
        "minute_execution": TypeAdapter(dict[str, MinuteExecutionRecord]).json_schema(),
        "storage_dynamics": TypeAdapter(dict[str, StorageDynamicsTrajectory]).json_schema(
            mode="serialization"
        ),
    }
    for name, schema in schemas.items():
        path = Path(__file__).resolve().parents[1] / f"contracts/control/{name}.schema.json"
        content = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"control contract schema drift: {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()

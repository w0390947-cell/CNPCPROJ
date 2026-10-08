"""Export fixed-state comparison v1/v2 input schemas from the authority types."""

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from oilfield_energy.power_flow_comparison import ComparisonRequest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema = TypeAdapter(ComparisonRequest).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-generator"] = "tools/export_comparison_contract.py v1; do not edit"
    schema["description"] = (
        "Versioned fixed-state input. Wind capability curves require v2; v1 is "
        "retained for historical inputs. Physical consistency and version checks "
        "also apply at runtime."
    )
    target = (
        Path(__file__).resolve().parents[1]
        / "contracts/power_flow/fixed_device_comparison.schema.json"
    )
    content = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            raise SystemExit("fixed-state comparison schema drift")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()

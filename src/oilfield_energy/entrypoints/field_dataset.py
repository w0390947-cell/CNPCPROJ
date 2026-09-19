"""CLI for capture validation, explicit sealing and fixed-state AC solving."""

import argparse
from datetime import datetime
from pathlib import Path
from typing import cast

from oilfield_energy.bootstrap.field_dataset import create_field_dataset_workflow
from oilfield_energy.workflows.field_dataset.contracts import Operation


def main() -> None:
    parser = argparse.ArgumentParser(description="Versioned field dataset workflow (no redispatch)")
    parser.add_argument("operation", choices=("check", "seal", "run"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new, unique output directory")
    parser.add_argument("--at", required=True, help="ISO target timestamp including timezone")
    parser.add_argument("--mode", help="explicit operating mode; otherwise use dataset default")
    parser.add_argument("--demo", action="store_true", help="explicit synthetic dataset mode")
    args = parser.parse_args()
    try:
        at = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if at.utcoffset() is None:
            parser.error("--at must include a timezone")
        workflow = create_field_dataset_workflow()
        outcome = workflow.execute(
            args.manifest,
            args.output,
            operation=cast(Operation, args.operation),
            at=at,
            mode=args.mode,
            demo=args.demo,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Field dataset error: {exc}\n")
    print(f"Field dataset: {outcome.status}; results: {outcome.output.resolve()}")
    raise SystemExit(outcome.exit_code)


if __name__ == "__main__":
    main()

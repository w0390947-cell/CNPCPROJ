"""Generate or run a reproducible synthetic Shancheng dataset."""

import argparse
from pathlib import Path

from oilfield_energy.bootstrap.shancheng_simulation import create_simulation, generate_simulation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--config", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "generate":
            generate_simulation(args.config, args.output)
            print(f"Synthetic dataset generated: {args.output}")
        else:
            result = create_simulation().execute(args.manifest, args.output)
            print(f"Shancheng synthetic study: {result.status}; {result.output}")
            raise SystemExit(result.exit_code)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Simulation input/output error: {exc}\n")


if __name__ == "__main__":
    main()

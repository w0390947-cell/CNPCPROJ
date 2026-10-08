"""Generate sealed synthetic study inputs without running an execution algorithm."""

import argparse
from pathlib import Path

from oilfield_energy.bootstrap.study_inputs import generate_study_inputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, help="Derived study recipe; defaults to packaged data"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        generate_study_inputs(args.config, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Study input/output error: {exc}\n")
    print(f"Synthetic dataset generated: {args.output}")


if __name__ == "__main__":
    main()

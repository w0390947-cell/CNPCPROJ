"""Installed-package worker; no source-directory path injection."""

import argparse
from pathlib import Path

from oilfield_energy.bootstrap.demo import run_demo_session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--managed", action="store_true")
    args = parser.parse_args()
    run_demo_session(args.session.resolve(), managed=args.managed)


if __name__ == "__main__":
    main()

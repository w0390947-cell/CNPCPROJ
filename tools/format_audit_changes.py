"""Format changed ranges against an explicit audit backup, without a Git checkout."""

import argparse
import difflib
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    before = args.before.read_text(encoding="utf-8").splitlines()
    after = args.after.read_text(encoding="utf-8").splitlines()
    ranges = [
        (j1 + 1, j2 + 1)
        for tag, _, _, j1, j2 in difflib.SequenceMatcher(
            None, before, after, autojunk=False
        ).get_opcodes()
        if tag != "equal" and j1 != j2
    ]
    original = args.after.read_bytes()
    for start, end in reversed(ranges):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "format",
                str(args.after),
                "--config",
                str(args.config),
                "--range",
                f"{start}-{end}",
                "--quiet",
            ],
            check=True,
        )
    if args.check and args.after.read_bytes() != original:
        args.after.write_bytes(original)
        raise SystemExit("changed-range formatting drift")
    print(
        f"{args.after.name}: {len(ranges)} changed ranges {'checked' if args.check else 'formatted'}"
    )


if __name__ == "__main__":
    main()

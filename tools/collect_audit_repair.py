"""Record current package hashes and source changes against a prior wheel."""

import argparse
import difflib
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--before-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifests: dict[str, str] = {}
    changes: list[str] = []
    differences: list[str] = []
    with ZipFile(args.before_wheel) as old:
        names = set(old.namelist())
        for path in sorted((args.source / "oilfield_energy").rglob("*.py")):
            key = path.relative_to(args.source).as_posix()
            manifests[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            before = old.read(key).decode("utf-8") if key in names else ""
            after = path.read_text(encoding="utf-8")
            if before.replace("\r", "") != after.replace("\r", ""):
                changes.append(str(path))
                differences.extend(
                    difflib.unified_diff(
                        before.splitlines(True),
                        after.splitlines(True),
                        fromfile=key + " (prior wheel)",
                        tofile=key + " (repaired source)",
                    )
                )
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("source_manifest_after_fix", manifests),
        ("changed_source_files", changes),
    ):
        (args.output / (name + ".json")).write_text(json.dumps(value, indent=2), encoding="utf-8")
    (args.output / "repair_changes.diff").write_text("".join(differences), encoding="utf-8")
    print(f"{len(manifests)} source files; {len(changes)} changed or added")


if __name__ == "__main__":
    main()

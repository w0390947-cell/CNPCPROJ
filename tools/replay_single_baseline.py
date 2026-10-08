"""Verify and replay frozen source or read old results in an isolated process."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, default=root / "archives/single_retirement_20261005"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--read-result", type=Path, help="Validate old JSON using archived types")
    args = parser.parse_args()
    archive = args.archive.resolve()
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    source = archive / "source.zip"
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("historical archive digest differs")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cnpc-single-baseline-") as temporary:
        workspace = Path(temporary).resolve()
        with zipfile.ZipFile(source) as bundle:
            if set(bundle.namelist()) != set(manifest["files"]):
                raise ValueError("historical archive membership differs")
            for name, digest in manifest["files"].items():
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                    raise ValueError("unsafe archive member")
                target = (workspace / Path(*relative.parts)).resolve()
                if not target.is_relative_to(workspace):
                    raise ValueError("archive target escaped the isolated workspace")
                content = bundle.read(name)
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError("historical source digest differs: " + name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        environment = {
            **os.environ,
            "PYTHONPATH": str(workspace / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "--basetemp",
            str(workspace / "pytest-tmp"),
            "tests/unit/control/test_device_tube.py",
            "tests/unit/power_flow/test_ac_envelope.py",
            "tests/workflows/test_conditional_guarantee.py",
            "-q",
        ]
        if args.read_result is not None:
            command = [
                sys.executable,
                "-B",
                "-c",
                "from pathlib import Path; import sys; "
                "from oilfield_energy.service import SimulationResult; "
                "r = SimulationResult.model_validate_json(Path(sys.argv[1]).read_bytes()); "
                "Path(sys.argv[2]).write_text(r.model_dump_json(indent=2), encoding='utf-8'); "
                "print('Historical result validated using archived types; no new guarantees assigned')",
                str(args.read_result.resolve()),
                str((args.output / "historical-result.json").resolve()),
            ]
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        (args.output / "process.txt").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
        report = {
            "archive_sha256": manifest["archive_sha256"],
            "source_files_verified": len(manifest["files"]),
            "python": sys.version,
            "command": command,
            "exit_code": completed.returncode,
            "scope": "historical result read"
            if args.read_result
            else "historical regression tests",
            "guarantees": "archived semantics only; no current unified guarantee assigned",
        }
        (args.output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(completed.stdout, end="")
        # TemporaryDirectory owns exactly this verified, newly created directory.
        if not workspace.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise RuntimeError("temporary cleanup target is outside the temporary root")
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()

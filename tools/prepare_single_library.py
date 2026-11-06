"""Compute a genuine full-day example, or preserve an existing compatible result.

The library is separate from temporary jobs. Existing records need --replace.
"""
import argparse
import os
import tempfile
from pathlib import Path

from oilfield_energy.bootstrap.adapters.single_library import library_path, validate_example
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation


def save(payload: bytes, *, replace: bool = False) -> Path:
    validate_example(SimulationResult.model_validate_json(payload))
    path = library_path()
    if path.exists() and not replace:
        raise FileExistsError(f"{path} already exists; use --replace explicitly")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if library_path().exists() and not args.replace:
        raise FileExistsError("example already exists; use --replace explicitly")
    if args.result:
        payload = args.result.read_bytes()
    else:
        result = run_simulation(
            SimulationRequest(
                name="单微网固定实施例 · 全天示范计算", steps=8, region="SC",
                storage_enabled=True, events=[],
                solver={"formulation": "misocp", "quality_policy": "quality-first-v1"},
            ),
            lambda row: print(row.label, flush=True),
        )
        payload = result.model_dump_json().encode("utf-8")
    print(save(payload, replace=args.replace), flush=True)


if __name__ == "__main__":
    main()

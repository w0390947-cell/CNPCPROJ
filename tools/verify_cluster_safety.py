"""Replay a saved v6 result or ClusterSafetyEvidence using the installed package."""

import argparse
import json
import hashlib
import sys
from pathlib import Path
from zipfile import ZipFile

import oilfield_energy

from oilfield_energy.workflows.cluster_execution.api import replay_cluster_safety
from oilfield_energy.workflows.cluster_execution.contracts import ClusterSafetyEvidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    installed = Path(oilfield_energy.__file__).resolve().parent
    sources = 0
    if args.source_root is not None or args.wheel is not None:
        if args.source_root is None or args.wheel is None:
            raise ValueError("wheel verification requires both source root and wheel")
        assert installed.is_relative_to(Path(sys.prefix).resolve())
        assert not any(
            Path(p).resolve() == (args.source_root / "src").resolve() for p in sys.path
        )
        source_files = {
            p.relative_to(args.source_root / "src").as_posix(): p.read_bytes()
            for p in (args.source_root / "src/oilfield_energy").rglob("*.py")
        }
        with ZipFile(args.wheel) as wheel:
            assert {
                n
                for n in wheel.namelist()
                if n.startswith("oilfield_energy/") and n.endswith(".py")
            } == set(source_files)
            for name, content in source_files.items():
                assert wheel.read(name) == content, name
                assert (installed.parent / name).read_bytes() == content, name
        sources = len(source_files)
    raw = json.loads(args.evidence.read_text(encoding="utf-8"))
    if "cluster_execution" in raw:
        raw = raw["cluster_execution"]["safety"]
    elif "safety" in raw:
        raw = raw["safety"]
    evidence = ClusterSafetyEvidence.model_validate(raw)
    minutes = replay_cluster_safety(evidence)
    report = {
        "context_id": evidence.domain.context_id,
        "replayed_minutes": minutes,
        "reserve_events": len(evidence.reserve_events),
        "installed_path": str(installed),
        "source_files_verified": sources,
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest()
        if args.wheel
        else None,
    }
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

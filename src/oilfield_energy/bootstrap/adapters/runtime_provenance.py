"""Read installed implementation identity at explicit workflow construction."""

import json
import platform
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from oilfield_energy.workflows.field_dataset.contracts import Artifact


def runtime_artifact(*, solver: str = "existing fixed-state AC backward-forward sweep") -> Artifact:
    package = Path(__file__).resolve().parents[2]
    source_files = [
        {
            "path": path.relative_to(package).as_posix(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(package.rglob("*.py"))
    ]
    dependencies: dict[str, str] = {}
    for name in (
        "oilfield-energy-optimization",
        "numpy",
        "scipy",
        "pydantic",
        "openpyxl",
        "cvxpy",
        "pyscipopt",
        "clarabel",
        "osqp",
    ):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "not-installed"
    data: dict[str, object] = {
        "schema_version": "field-runtime-v1",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": dependencies,
        "implementation_sha256": sha256(
            json.dumps(source_files, sort_keys=True).encode()
        ).hexdigest(),
        "implementation_files": source_files,
        "solver": solver,
        "random_generator": None,
        "random_seed": None,
        "reproducibility": "numerical tolerance; retain matching software separately",
    }
    return Artifact("runtime.json", json.dumps(data, indent=2, allow_nan=False).encode())

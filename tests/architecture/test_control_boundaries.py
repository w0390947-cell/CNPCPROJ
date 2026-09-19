"""Guard the new control rules and their versioned public projection."""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from pydantic import TypeAdapter

from oilfield_energy.modules.control.contracts import (
    MinuteExecutionRecord,
    StorageDynamicsTrajectory,
)

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = {"oilfield_energy.modules.control.api", "oilfield_energy.modules.control.contracts"}


def test_legacy_control_consumers_use_only_public_paths():
    for name in (
        "device_control",
        "hierarchy_types",
        "hierarchy_reporting",
        "shancheng_control",
        "hierarchical",
    ):
        path = ROOT / "src/oilfield_energy" / (name + ".py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    target = importlib.util.resolve_name(
                        "." * node.level + target, "oilfield_energy"
                    )
                if "modules.control" in target:
                    assert target in PUBLIC


def test_control_schema_is_generated_from_current_public_contract():
    schema = json.loads(
        (ROOT / "contracts/control/minute_execution.schema.json").read_text(encoding="utf-8")
    )
    assert schema == TypeAdapter(dict[str, MinuteExecutionRecord]).json_schema()
    dynamics = json.loads((ROOT / "contracts/control/storage_dynamics.schema.json").read_text())
    assert dynamics == TypeAdapter(dict[str, StorageDynamicsTrajectory]).json_schema(
        mode="serialization"
    )


def test_public_execution_api_does_not_load_numerical_or_http_sdks(tmp_path):
    script = """
import sys
class RejectHeavy:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cvxpy', 'pyscipopt', 'numpy', 'scipy', 'fastapi', 'matplotlib'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectHeavy())
from oilfield_energy.modules.control.api import limit_synthetic_generation
from oilfield_energy.modules.control.api import allocate_wind_storage_target
from oilfield_energy.modules.control.api import execution_substeps, constrain_storage_power
from oilfield_energy.modules.control.contracts import WindActiveState, StorageActiveCapability
assert limit_synthetic_generation(5., 1., 2.) == 1.
assert execution_substeps(15, 1) == 15
assert constrain_storage_power(previous_mw=0, requested_mw=2, minimum_mw=-2.5, maximum_mw=2.5, maximum_change_mw=.65).actual_mw == .65
assert allocate_wind_storage_target((WindActiveState('w', 3, 1, True),), StorageActiveCapability(0, True, 0, 0), 3) is None
"""
    process = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True
    )
    assert process.returncode == 0, process.stderr

"""Additional gates for the new dispatch increment and its legacy consumers."""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "src/oilfield_energy/modules/dispatch"
ALLOWED = {"oilfield_energy.modules.dispatch.api", "oilfield_energy.modules.dispatch.contracts"}


def forbidden_dependency(module: str) -> bool:
    head = module.split(".")[0]
    return head in {
        "pyscipopt",
        "cvxpy",
        "scipy",
        "fastapi",
        "os",
        "pathlib",
        "tests",
        "tools",
    } or (
        module.startswith("oilfield_energy.")
        and not module.startswith("oilfield_energy.modules.dispatch.")
    )


def test_new_dispatch_is_solver_and_io_independent():
    for path in DISPATCH.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert not any(forbidden_dependency(a.name) for a in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                assert not forbidden_dependency(node.module or "")


def test_legacy_consumers_only_use_dispatch_public_boundary():
    for name in ("model", "misocp_model", "regional_control", "hierarchy_types", "service", "admm"):
        path = ROOT / "src/oilfield_energy" / (name + ".py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    target = importlib.util.resolve_name(
                        "." * node.level + target, "oilfield_energy"
                    )
                if "modules.dispatch" in target:
                    assert target in ALLOWED


@pytest.mark.parametrize(
    "module",
    ["pyscipopt", "cvxpy", "scipy.optimize", "pathlib", "oilfield_energy.model", "tests.support"],
)
def test_negative_boundary_examples(module):
    assert forbidden_dependency(module)


def test_public_accounting_imports_work_outside_workspace_without_solver_imports(tmp_path):
    code = """
import sys
class RejectHeavy:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cvxpy','pyscipopt','scipy','fastapi','matplotlib'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectHeavy())
from oilfield_energy.modules.dispatch.api import account_renewable
assert account_renewable([2.], [1.], [0.]).curtailed_mw == (1.,)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

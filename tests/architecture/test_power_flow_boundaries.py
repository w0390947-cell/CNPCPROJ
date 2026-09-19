"""Numerical validation stays pure and legacy callers use only public APIs."""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = {"oilfield_energy.modules.power_flow.api", "oilfield_energy.modules.power_flow.contracts"}


def forbidden(module):
    return module.split(".")[0] in {
        "cvxpy",
        "pyscipopt",
        "scipy",
        "os",
        "pathlib",
        "fastapi",
        "tools",
        "tests",
    } or (
        module.startswith("oilfield_energy.")
        and not module.startswith("oilfield_energy.modules.power_flow.")
    )


def test_numerical_validation_has_no_io_solver_or_legacy_imports():
    for path in (ROOT / "src/oilfield_energy/modules/power_flow").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert not any(forbidden(a.name) for a in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                assert not forbidden(node.module or "")


@pytest.mark.parametrize(
    "module",
    ["scipy.optimize", "oilfield_energy.network_model", "cvxpy", "pathlib", "tests.support"],
)
def test_forbidden_import_examples(module):
    assert forbidden(module)


def test_legacy_numerics_use_registered_public_paths():
    for name in ("ac_power_flow", "network_scenarios"):
        path = ROOT / "src/oilfield_energy" / (name + ".py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    target = importlib.util.resolve_name(
                        "." * node.level + target, "oilfield_energy"
                    )
                if "modules.power_flow" in target:
                    assert target in PUBLIC


def test_versioned_scenario_schema_has_no_generation_drift():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/export_network_scenario_contract.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

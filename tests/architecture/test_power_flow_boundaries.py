"""Numerical validation stays pure and legacy callers use only public APIs."""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = {
    "oilfield_energy.modules.power_flow.api",
    "oilfield_energy.modules.power_flow.contracts",
}


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
        and module
        not in {"oilfield_energy.kernel.enclosures", "oilfield_energy.kernel.intervals"}
    )


def test_numerical_validation_has_no_io_solver_or_legacy_imports():
    for path in (ROOT / "src/oilfield_energy/modules/power_flow").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert not any(forbidden(a.name) for a in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                assert not forbidden(node.module or "")


def test_shared_interval_primitives_do_not_hide_io_or_business_dependencies():
    for path in (ROOT / "src/oilfield_energy/kernel").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(
                    a.name.split(".")[0] in {"math", "fractions", "pydantic"}
                    for a in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                assert node.module in {"math", "fractions", "pydantic"}


@pytest.mark.parametrize(
    "module",
    [
        "scipy.optimize",
        "oilfield_energy.network_model",
        "cvxpy",
        "pathlib",
        "tests.support",
    ],
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
        [
            sys.executable,
            str(ROOT / "tools/export_network_scenario_contract.py"),
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_device_checker_uses_public_resource_contract_and_does_not_redispatch():
    path = ROOT / "src/oilfield_energy/power_flow_comparison.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    resource_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and "modules.resources" in (node.module or "")
    ]
    assert resource_imports == ["modules.resources.contracts"]
    checker = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "device_capability_violations"
    )
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(checker)
    )
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(checker)
        if isinstance(node, ast.Call)
    }
    assert "wind.limit_at" in calls
    assert not any("solve" in name or "clip" in name for name in calls)

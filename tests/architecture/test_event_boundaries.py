"""Event semantics remain owned by studies; legacy callers use its public boundary."""

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def permitted_import(module):
    return module in {
        "collections.abc",
        "oilfield_energy.modules.studies.contracts",
    }


def test_event_rules_have_no_legacy_solver_or_io_dependencies():
    path = ROOT / "src/oilfield_energy/modules/studies/application_events.py"
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = importlib.util.resolve_name(
                    "." * node.level + name, "oilfield_energy.modules.studies"
                )
            assert permitted_import(name)
        elif isinstance(node, ast.Import):
            assert all(permitted_import(alias.name) for alias in node.names)


@pytest.mark.parametrize(
    "module",
    [
        "oilfield_energy.service",
        "oilfield_energy.communication",
        "pyscipopt",
        "numpy.random",
        "pathlib",
    ],
)
def test_event_rule_forbidden_import_examples(module):
    assert not permitted_import(module)


def test_legacy_callers_use_only_public_study_contracts_and_api():
    for name in ("service", "scenario_events", "communication", "hierarchy_types"):
        path = ROOT / "src/oilfield_energy" / f"{name}.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and "modules.studies" in (node.module or ""):
                assert (node.module or "").rsplit(".", 1)[-1] in {"api", "contracts"}

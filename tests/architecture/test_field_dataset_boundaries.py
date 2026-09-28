"""Executable dependency boundaries for the new increment (not a whole-repo audit)."""

import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src/oilfield_energy"
REGISTRY = tomllib.loads((ROOT / "docs/architecture/modules.toml").read_text(encoding="utf-8"))
PREFIX = "oilfield_energy."


def imports(text, module):
    package = module.rpartition(".")[0]
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                target = importlib.util.resolve_name("." * node.level + target, package)
            yield target


def violation(source, target):
    if target.split(".")[0] in {"tests", "examples", "experiments", "tools"}:
        return True
    if not target.startswith(PREFIX):
        return False
    if source.startswith(PREFIX + "runtime."):
        return not target.startswith(PREFIX + "runtime.")
    for item in REGISTRY["modules"]:
        own = PREFIX + "modules." + item["name"] + "."
        if source.startswith(own):
            if not target.startswith(own):
                allowed = {
                    PREFIX + "modules." + name + ".contracts" for name in item["allow_contracts"]
                }
                if source[len(own) :].startswith(("api", "application")):
                    allowed |= {PREFIX + "modules." + name + ".api" for name in item["allow_apis"]}
                return target not in allowed
            source_part, target_part = source[len(own) :], target[len(own) :]
            if source_part == "contracts":
                return target_part != "contracts"
            if source_part.startswith("domain") and target_part not in {"contracts", "domain"}:
                return not target_part.startswith("domain.")
            return source_part.startswith(("application", "api")) and target_part.startswith(
                "adapters."
            )
    for item in REGISTRY["components"]:
        if "/workflows/" not in item["path"]:
            continue
        workflow = item["path"].removeprefix("src/").replace("/", ".") + "."
        if source.startswith(workflow):
            allowed = {PREFIX + "modules." + name for name in item.get("allow_public", [])}
            allowed |= {PREFIX + "workflows." + name for name in item.get("allow_workflows", [])}
            return not (target.startswith(workflow) or target in allowed)
    if source.startswith(PREFIX + "entrypoints."):
        return not (
            target.startswith(PREFIX + "bootstrap.")
            or target == PREFIX + "workflows.field_dataset.contracts"
        )
    if source.startswith(PREFIX + "bootstrap."):
        if target.startswith((PREFIX + "modules.", PREFIX + "workflows.", PREFIX + "bootstrap.", PREFIX + "runtime.")):
            return False
        return not any(
            source == bridge["path"].removeprefix("src/").removesuffix(".py").replace("/", ".")
            and target in bridge["imports"]
            for bridge in REGISTRY["legacy_bridges"]
        )
    return True


def sources():
    for area in ("modules", "workflows", "bootstrap", "entrypoints", "runtime"):
        for path in (PACKAGE / area).rglob("*.py"):
            yield (
                path,
                PREFIX + path.relative_to(PACKAGE).with_suffix("").as_posix().replace("/", "."),
            )


def test_registered_paths_owners_and_public_modules():
    modules = REGISTRY["modules"]
    assert {
        p.name for p in (PACKAGE / "modules").iterdir() if p.is_dir() and p.name != "__pycache__"
    } == {item["name"] for item in modules}
    for item in modules + REGISTRY["components"]:
        path = ROOT / item["path"]
        assert path.is_dir() and item["owners"] and item["reviewers"]
        assert (path / "README.md").is_file()
        extension = {"python": ".py", "typescript": ".ts"}[item.get("language", "python")]
        for public in item.get("public_modules", []):
            assert (path / (public + extension)).is_file()


def test_dependency_graph_and_no_dynamic_bypass():
    graph = {}
    for path, name in sources():
        text = path.read_text(encoding="utf-8")
        dependencies = set(imports(text, name))
        assert not [(name, target) for target in dependencies if violation(name, target)]
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call):
                called = ast.unparse(node.func)
                assert called not in {
                    "eval",
                    "exec",
                    "__import__",
                    "import_module",
                    "importlib.import_module",
                }
            if isinstance(node, ast.Attribute):
                assert ast.unparse(node) != "sys.path"
        graph[name] = dependencies
    visited, active = set(), set()

    def visit(name):
        assert name not in active, f"dependency cycle: {name}"
        if name in visited:
            return
        active.add(name)
        for target in graph.get(name, set()):
            visit(target)
        active.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)


def test_checker_positive_and_negative_examples():
    module = PREFIX + "modules.measurements."
    workflow = PREFIX + "workflows.field_dataset."
    assert not violation(workflow + "api", module + "api")
    assert not violation(module + "application.capture", module + "domain")
    assert violation(workflow + "api", module + "adapters.local_files")
    assert violation(module + "domain", module + "adapters.local_files")
    assert violation(module + "api", workflow + "api")
    assert violation(workflow + "api", PREFIX + "model")
    assert violation(module + "domain", "tests.helpers")
    assert violation(PREFIX + "runtime.periodic", PREFIX + "modules.demo_simulation.api")
    assert not violation(PREFIX + "bootstrap.demo", PREFIX + "runtime.periodic")


def test_contract_import_does_not_load_solvers_or_open_files(tmp_path):
    script = """
import sys
class RejectHeavy:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cvxpy', 'scipy', 'matplotlib', 'pyscipopt'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectHeavy())
from oilfield_energy.modules.measurements.contracts import DatasetManifest
print(DatasetManifest.__name__)
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    process = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert process.returncode == 0, process.stderr


def test_legacy_root_exports_remain_resolvable():
    import oilfield_energy

    assert len(oilfield_energy.__all__) == 123
    assert set(oilfield_energy.__all__) == set(oilfield_energy._EXPORTS)
    for name, (module, original) in oilfield_energy._EXPORTS.items():
        assert getattr(oilfield_energy, name) is getattr(
            importlib.import_module(PREFIX + module), original
        )
    assert set(oilfield_energy.__all__) <= set(dir(oilfield_energy))


def test_generated_schemas_match_authoritative_types():
    from oilfield_energy.modules.measurements.contracts import DatasetManifest
    from oilfield_energy.workflows.field_dataset.adapters.document_assembly import DocumentEnvelope

    for name, model in (("manifest", DatasetManifest), ("document", DocumentEnvelope)):
        schema = json.loads(
            (ROOT / f"contracts/field_dataset/{name}.schema.json").read_text(encoding="utf-8")
        )
        assert schema == model.model_json_schema()

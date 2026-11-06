"""Saved-example identity, completeness and read-only HTTP boundary."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI

from oilfield_energy.bootstrap.adapters import single_library as library
from oilfield_energy.service import SimulationResult
from oilfield_energy.web_api import router


def identity_fixture():
    """Identity-only stand-in; these rows are not simulated trajectories."""
    inputs = library.describe_single_example()
    return NS(
        request=NS(scenario_type="single_microgrid", region="SC", steps=8,
                   storage_enabled=True, events=[], single_boundary=None, single_research=None),
        metadata=NS(scenario_type="single_microgrid", region="SC", steps=8,
                    dt_hours=3, dataset_sha256=inputs.dataset_sha256),
        single_execution=NS(total_minutes=1440, completed_minutes=1440,
                            unified=NS(requested_steps=1440, update_steps=inputs.update_steps,
                                       domain=inputs.domain, initial=inputs.initial,
                                       steps=[NS(step=i) for i in range(1440)])),
    )


def test_identity_accepts_matching_inputs_without_rewriting_historical_context():
    result = identity_fixture()
    u = result.single_execution.unified
    u.domain = u.domain.model_copy(update={
        "context_id": "historical-context",
        "devices": u.domain.devices.model_copy(update={"context_id": "historical-context"}),
    })
    library.validate_example(result)
    assert u.domain.context_id == "historical-context"


@pytest.mark.parametrize("change", ["partial", "sequence", "forecast", "initial", "dataset", "request", "context"])
def test_incompatible_or_incomplete_examples_are_rejected(change):
    result = identity_fixture()
    u = result.single_execution.unified
    if change == "partial":
        result.single_execution.completed_minutes = 20
    elif change == "sequence":
        u.steps[20].step = 19
    elif change == "forecast":
        u.domain = u.domain.model_copy(update={"forecasts": u.domain.forecasts[:-1]})
    elif change == "initial":
        u.initial = u.initial.model_copy(update={"energy_mwh": 0.5})
    elif change == "dataset":
        result.metadata.dataset_sha256 = "another-dataset"
    elif change == "request":
        result.request.single_research = {}
    else:
        u.domain = u.domain.model_copy(update={"context_id": "mismatch"})
    with pytest.raises(ValueError):
        library.validate_example(result)


async def get(app, path):
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    await app({"type": "http", "method": "GET", "path": path, "query_string": b"",
               "headers": [], "scheme": "http", "server": ("test", 80),
               "client": ("test", 1), "root_path": "", "http_version": "1.1"}, receive, send)
    return (next(m["status"] for m in messages if m["type"] == "http.response.start"),
            json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")))


def test_http_lists_only_durable_records_and_reports_missing_or_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "LIBRARY_ROOT", tmp_path)
    app = FastAPI()
    app.include_router(router)
    async def check():
        assert await get(app, "/api/single-example/library") == (200, [])
        assert (await get(app, "/api/single-example/library/core"))[0] == 404
        assert (await get(app, "/api/single-example/library/unknown"))[0] == 422
        library.library_path().write_text("{}", encoding="utf-8")
        assert await get(app, "/api/single-example/library") == (200, ["core"])
        assert (await get(app, "/api/single-example/library/core"))[0] == 422
    asyncio.run(check())


@pytest.mark.skipif(not os.environ.get("SINGLE_DEMO_ARCHIVE"), reason="requires a genuine full-day archive")
def test_actual_archive_preserves_bytes_timestamp_and_assessments(tmp_path, monkeypatch):
    payload = Path(os.environ["SINGLE_DEMO_ARCHIVE"]).read_bytes()
    original = SimulationResult.model_validate_json(payload)
    monkeypatch.setattr(library, "LIBRARY_ROOT", tmp_path)
    spec = importlib.util.spec_from_file_location("prepare_single_library", Path(__file__).resolve().parents[3] / "tools/prepare_single_library.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    assert prepare.save(payload).read_bytes() == payload
    assert library.read_library() == original
    with pytest.raises(FileExistsError):
        prepare.save(payload)
    assert prepare.save(payload, replace=True).read_bytes() == payload
    app = FastAPI()
    app.include_router(router)
    code, response = asyncio.run(get(app, "/api/single-example/library/core"))
    assert code == 200
    assert SimulationResult.model_validate(response) == original

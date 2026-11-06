"""Snapshot transport contracts, durable replay and storage failure semantics."""

import asyncio
import sqlite3

from oilfield_energy.bootstrap.field_initialization import (
    create_field_initializer,
    create_snapshot_submitter,
)
from oilfield_energy.web_api import create_app
from oilfield_energy.workflows.field_initialization.contracts import InitializeResponse
from tests.support.field_http import post
from tests.support.field_initialization import NOW, request_document
from tests.support.field_planning import initialized
from tests.support.field_snapshots import snapshot_document

URL = "/api/field/single-microgrid/snapshots"


def test_http_snapshot_assessment_and_restart_replay(tmp_path):
    async def scenario():
        app = create_app(tmp_path)
        root = tmp_path / "field-initialization"
        async with app.router.lifespan_context(app):
            app.state.field_initializer = create_field_initializer(
                root, clock=lambda: NOW
            )
            app.state.snapshot_submitter = create_snapshot_submitter(
                root, clock=lambda: NOW
            )
            original = request_document()
            code, response = await post(app, original)
            assert code == 200
            d = snapshot_document(original, InitializeResponse.model_validate(response))
            code, accepted = await post(app, d, path=URL)
            assert code == 200, accepted
            assert accepted["payload"]["data"]["state_status"] == "consistent"
            assert accepted["payload"]["data"]["execution_confirmed"] is False
            app.state.snapshot_submitter = create_snapshot_submitter(
                root, clock=lambda: NOW
            )
            assert await post(app, d, path=URL) == (code, accepted)
            d["payload"]["pcc"]["p_import_mw"]["value"] = 2.0
            code, rejected = await post(app, d, path=URL)
            assert code == 409
            assert rejected["payload"]["errors"][0]["code"] == "DUPLICATE_ID_CONFLICT"
            spec = app.openapi()["paths"][URL]["post"]
            assert spec["requestBody"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("SubmitSnapshotRequest")

    asyncio.run(scenario())


def test_http_snapshot_strict_json_and_unknown_fields(tmp_path):
    original, init = initialized(tmp_path / "fixture")

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e400}', b"not json"):
                code, _ = await post(app, raw=raw, path=URL)
                assert code == 422
            for bad in ("1.0", True):
                d = snapshot_document(original, init)
                d["payload"]["pcc"]["p_import_mw"]["value"] = bad
                assert (await post(app, d, path=URL))[0] == 422
            for key, value in (("context_id", None), ("extra", 1), ("sequence", True)):
                d = snapshot_document(original, init)
                d["payload"][key] = value
                assert (await post(app, d, path=URL))[0] == 422
            code, _ = await post(app, raw=b" " * (8 * 1024 * 1024 + 1), path=URL)
            assert code == 413

    asyncio.run(scenario())


def test_http_snapshot_storage_failure_never_returns_success(tmp_path):
    original, init = initialized(tmp_path / "fixture")

    class Unavailable:
        def execute(self, request):
            raise sqlite3.OperationalError("test storage failure")

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            app.state.snapshot_submitter = Unavailable()
            code, response = await post(
                app, snapshot_document(original, init), path=URL
            )
            assert code == 503
            assert response["code"] == "SESSION_STORE_UNAVAILABLE"

    asyncio.run(scenario())

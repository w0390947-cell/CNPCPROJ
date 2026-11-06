"""Shadow preparation HTTP/schema handling without a simulation run."""

import asyncio
import sqlite3

from oilfield_energy.bootstrap.field_initialization import create_control_cycle_preparer
from oilfield_energy.web_api import create_app
from tests.support.field_cycles import cycle_document
from tests.support.field_http import post
from tests.support.field_initialization import NOW
from tests.support.field_planning import initialized

URL = "/api/field/single-microgrid/control-cycles/prepare"


def test_http_shadow_preparation_replay_and_conflict(tmp_path):
    root = tmp_path / "field-initialization"
    original, init = initialized(root)
    doc = cycle_document(original, init)

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            app.state.control_cycle_preparer = create_control_cycle_preparer(
                root, clock=lambda: NOW
            )
            code, result = await post(app, doc, path=URL)
            assert code == 200, result
            assert result["payload"]["data"]["decision"] == "shadow_only"
            app.state.control_cycle_preparer = create_control_cycle_preparer(
                root, clock=lambda: NOW
            )
            assert await post(app, doc, path=URL) == (code, result)
            doc["payload"]["snapshot_id"] = "changed"
            code, result = await post(app, doc, path=URL)
            assert (
                code == 409
                and result["payload"]["errors"][0]["code"] == "DUPLICATE_ID_CONFLICT"
            )
            spec = app.openapi()["paths"][URL]["post"]
            assert spec["requestBody"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("PrepareControlCycleRequest")

    asyncio.run(scenario())


def test_http_rejects_malformed_input_and_closed_loop_authority(tmp_path):
    original, init = initialized(tmp_path / "fixture")

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e400}', b"not json"):
                assert (await post(app, raw=raw, path=URL))[0] == 422
            for value in ("false", 0):
                doc = cycle_document(original, init)
                doc["payload"]["authority"]["write_enabled"] = value
                assert (await post(app, doc, path=URL))[0] == 422
            doc = cycle_document(original, init)
            doc["payload"]["authority"]["write_enabled"] = True
            assert (await post(app, doc, path=URL))[0] == 409
            doc = cycle_document(original, init)
            doc["payload"]["cycle_start_at"] = "2026-10-15T02:00:00"
            assert (await post(app, doc, path=URL))[0] == 422
            assert (await post(app, raw=b" " * (8 * 1024 * 1024 + 1), path=URL))[
                0
            ] == 413

    asyncio.run(scenario())


def test_http_persistence_failure_is_unavailable_not_success(tmp_path):
    original, init = initialized(tmp_path / "fixture")

    class Unavailable:
        def execute(self, request):
            raise sqlite3.OperationalError("test failure")

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            app.state.control_cycle_preparer = Unavailable()
            assert await post(app, cycle_document(original, init), path=URL) == (
                503,
                {"code": "SESSION_STORE_UNAVAILABLE"},
            )

    asyncio.run(scenario())

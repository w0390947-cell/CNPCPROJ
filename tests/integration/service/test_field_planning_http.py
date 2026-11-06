"""HTTP updates use durable sessions and never invoke the simulation endpoint."""

import asyncio
import sqlite3

from oilfield_energy.bootstrap.field_initialization import (
    create_field_initializer,
    create_planning_updater,
)
from oilfield_energy.web_api import create_app
from oilfield_energy.workflows.field_initialization.contracts import InitializeResponse
from tests.support.field_initialization import NOW, request_document
from tests.support.field_planning import update_document
from tests.support.field_http import post

URL = "/api/field/single-microgrid/planning-inputs"


def test_http_stage_activate_replay_and_old_context_rejection(tmp_path):
    async def scenario():
        app = create_app(tmp_path)
        root = tmp_path / "field-initialization"
        async with app.router.lifespan_context(app):
            app.state.field_initializer = create_field_initializer(
                root, clock=lambda: NOW
            )
            app.state.planning_updater = create_planning_updater(
                root, clock=lambda: NOW
            )
            doc = request_document()
            code, initial = await post(app, doc)
            assert code == 200
            init = InitializeResponse.model_validate(initial)
            staged = update_document(doc, init, offset_minutes=15)
            code, result = await post(app, staged, path=URL)
            assert code == 202 and result["payload"]["data"]["new_context_id"] is None
            assert await post(app, staged, path=URL) == (code, result)
            active = update_document(doc, init)
            active["message_id"] = "activate"
            active["payload"]["input_revision"] = "forecast-3"
            code, result = await post(app, active, path=URL)
            assert code == 200, result
            assert result["payload"]["data"]["activation_status"] == "activated"
            assert result["payload"]["data"]["dispatch_enabled"] is False
            app.state.planning_updater = create_planning_updater(
                root, clock=lambda: NOW
            )
            assert await post(app, active, path=URL) == (code, result)
            active["message_id"] = "old-context"
            active["payload"]["input_revision"] = "forecast-4"
            code, failure = await post(app, active, path=URL)
            assert code == 409
            assert failure["payload"]["errors"][0]["code"] == "CONTEXT_CHANGED"
            spec = app.openapi()["paths"][URL]["post"]
            assert "202" in spec["responses"] and "409" in spec["responses"]
            assert spec["requestBody"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("UpdatePlanningRequest")

    asyncio.run(scenario())


def test_http_strict_update_messages(tmp_path):
    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            for raw in (
                b'{"payload": {}, "payload": {}}',
                b'{"x": NaN}',
                b'{"x": 1e400}',
            ):
                assert (await post(app, raw=raw, path=URL))[0] == 422
            # No initialized run is needed to reject structurally malformed input.
            from tests.support.field_planning import initialized

            doc, init = initialized(tmp_path / "fixture")
            d = update_document(doc, init)
            d["payload"]["cost_rates"]["loss_cny_per_mwh"] = "20.0"
            assert (await post(app, d, path=URL))[0] == 422
            d = update_document(doc, init)
            d["payload"]["context_id"] = None
            assert (await post(app, d, path=URL))[0] == 422
            d = update_document(doc, init)
            d["payload"]["mode"] = "closed_loop"
            assert (await post(app, d, path=URL))[0] == 422
            assert (await post(app, raw=b" " * (8 * 1024 * 1024 + 1), path=URL))[
                0
            ] == 413

    asyncio.run(scenario())


def test_http_storage_failure_is_not_reported_as_activation(tmp_path, monkeypatch):
    from oilfield_energy.workflows.field_initialization.api import UpdatePlanningInputs
    from tests.support.field_planning import initialized

    doc, init = initialized(tmp_path / "fixture")

    def unavailable(self, request):
        raise sqlite3.OperationalError("injected unavailable store")

    monkeypatch.setattr(UpdatePlanningInputs, "execute", unavailable)

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            code, failure = await post(app, update_document(doc, init), path=URL)
            assert code == 503 and failure["code"] == "SESSION_STORE_UNAVAILABLE"

    asyncio.run(scenario())

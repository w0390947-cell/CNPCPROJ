"""Exercise the ASGI boundary and real lifespan without optional HTTP clients."""

import asyncio

from oilfield_energy.bootstrap.field_initialization import create_field_initializer
from oilfield_energy.web_api import create_app
from tests.support.field_initialization import NOW, request_document
from tests.support.field_http import post

URL = "/api/field/single-microgrid/initialize"


def test_http_initialization_and_exact_replay(tmp_path):
    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            app.state.field_initializer = create_field_initializer(
                tmp_path / "field-initialization", clock=lambda: NOW
            )
            status, result = await post(app, request_document())
            assert status == 200, result
            assert result["payload"]["data"]["dispatch_enabled"] is False
            assert await post(app, request_document()) == (status, result)
            d = request_document()
            d["message_id"] = "new-message"
            assert (await post(app, d))[0] == 409
            schema = app.openapi()
            assert schema["paths"][URL]["post"]["requestBody"]["content"][
                "application/json"
            ]["schema"]["$ref"].endswith("InitializeRequest")

    asyncio.run(scenario())


def test_http_rejects_duplicate_keys_unknown_fields_and_nonfinite_json(tmp_path):
    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            assert (
                await post(
                    app, raw=b'{"operation":"InitializeRun","operation":"other"}'
                )
            )[0] == 422
            assert (await post(app, raw=b'{"value": NaN}'))[0] == 422
            assert (await post(app, raw=b'{"value": 1e400}'))[0] == 422
            d = request_document()
            d["unknown"] = 1
            assert (await post(app, d))[0] == 422
            d = request_document()
            d["payload"]["control_policy"]["alpha_q"] = "0.5"
            assert (await post(app, d))[0] == 422

    asyncio.run(scenario())


def test_http_closed_loop_rejected_without_creating_ready_session(tmp_path):
    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            app.state.field_initializer = create_field_initializer(
                tmp_path / "field-initialization", clock=lambda: NOW
            )
            d = request_document()
            d["payload"]["mode"] = "closed_loop"
            status, result = await post(app, d)
            assert status == 409
            assert result["payload"]["errors"][0]["code"] == "CLOSED_LOOP_UNAVAILABLE"

    asyncio.run(scenario())

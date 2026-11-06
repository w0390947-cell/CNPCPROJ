"""PCC target HTTP contract and failure paths without a simulation request."""

import asyncio
import sqlite3

from oilfield_energy.bootstrap.field_initialization import (
    create_field_initializer,
    create_pcc_target_requester,
)
from oilfield_energy.web_api import create_app
from oilfield_energy.workflows.field_initialization.contracts import InitializeResponse
from tests.support.field_http import post
from tests.support.field_initialization import NOW
from tests.support.field_planning import initialized
from tests.support.field_targets import target_case, target_document

URL = "/api/field/single-microgrid/pcc-target"


def test_http_target_acceptance_replay_conflict_and_explicit_deadline(tmp_path):
    async def scenario():
        app = create_app(tmp_path)
        root = tmp_path / "field-initialization"
        async with app.router.lifespan_context(app):
            app.state.field_initializer = create_field_initializer(
                root, clock=lambda: NOW
            )
            app.state.pcc_target_requester = create_pcc_target_requester(
                root, clock=lambda: NOW
            )
            code, response = await post(app, target_case())
            assert code == 200
            d = target_document(InitializeResponse.model_validate(response))
            code, accepted = await post(app, d, path=URL)
            assert code == 200, accepted
            data = accepted["payload"]["data"]
            assert data["requested_target"] == data["accepted_target"]
            assert data["dispatch_enabled"] is False
            app.state.pcc_target_requester = create_pcc_target_requester(
                root, clock=lambda: NOW
            )
            assert await post(app, d, path=URL) == (code, accepted)
            d["payload"]["target_q_import_mvar"] = 0.01
            code, conflict = await post(app, d, path=URL)
            assert code == 409
            assert conflict["payload"]["errors"][0]["code"] == "DUPLICATE_ID_CONFLICT"
            assert (
                conflict["payload"]["data"]["previous_strategy_status"]
                == "not_assessed"
            )
            d["message_id"] = "target-conflict"
            code, conflict = await post(app, d, path=URL)
            assert code == 409
            assert conflict["payload"]["errors"][0]["code"] == "TARGET_ID_CONFLICT"
            assert conflict["payload"]["data"]["previous_target_id"] == "external-1"
            spec = app.openapi()["paths"][URL]["post"]
            assert spec["requestBody"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("RequestPccTargetRequest")

    asyncio.run(scenario())


def test_http_target_rejects_invalid_wire_values(tmp_path):
    _, init = initialized(tmp_path / "fixture", target_case())

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            for raw in (
                b'{"x": NaN}',
                b'{"x": 1e400}',
                b'{"operation":"RequestPccTarget","operation":"other"}',
            ):
                assert (await post(app, raw=raw, path=URL))[0] == 422
            for value in ("1.0", True, None):
                d = target_document(init)
                d["payload"]["target_p_import_mw"] = value
                assert (await post(app, d, path=URL))[0] == 422
            d = target_document(init)
            d["payload"]["unknown"] = 1
            assert (await post(app, d, path=URL))[0] == 422
            assert (await post(app, raw=b" " * (8 * 1024 * 1024 + 1), path=URL))[
                0
            ] == 413

    asyncio.run(scenario())


def test_http_target_storage_failure_is_not_an_acceptance(tmp_path, monkeypatch):
    from oilfield_energy.workflows.field_initialization.api import RequestPccTarget

    _, init = initialized(tmp_path / "fixture", target_case())

    def unavailable(self, request):
        raise sqlite3.OperationalError("injected unavailable store")

    monkeypatch.setattr(RequestPccTarget, "execute", unavailable)

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            code, result = await post(app, target_document(init), path=URL)
            assert code == 503 and result["code"] == "SESSION_STORE_UNAVAILABLE"

    asyncio.run(scenario())

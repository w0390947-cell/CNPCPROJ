"""Public recovery interfaces use the existing strict envelope and transaction."""

import asyncio
import sqlite3

from oilfield_energy.web_api import create_app
from oilfield_energy.workflows.field_initialization.contracts import ExecutionResponse
from tests.support.field_http import post
from tests.support.field_recovery import RecoveryHarness

PREFIX = "/api/field/single-microgrid/"


def test_http_explicit_prepare_resume_replay_and_strict_contract(tmp_path):
    h = RecoveryHarness(tmp_path / "field-initialization")
    h.stop()
    h.proposal = h.recovery_request()

    async def scenario():
        app = create_app(
            tmp_path,
            field_transport=h.transport,
            field_clock=lambda: h.now,
            field_recovery_verifier=h.verifier,
        )
        async with app.router.lifespan_context(app):
            request = h.proposal.model_dump(mode="json")
            code, body = await post(app, request, path=PREFIX + "resynchronizations/prepare")
            assert code == 202 and body["payload"]["data"]["run_state"] == "stopped", body
            candidate = ExecutionResponse.model_validate(body)
            resume = h.resume_request(candidate).model_dump(mode="json")
            committed = await post(app, resume, path=PREFIX + "resume")
            assert (
                committed[0] == 200 and committed[1]["payload"]["data"]["run_state"] == "ready"
            ), committed
            assert await post(app, resume, path=PREFIX + "resume") == committed
            # Historical preparation response is not a fresh state query or resume.
            assert (await post(app, request, path=PREFIX + "resynchronizations/prepare"))[0] == 202
            assert h.query().payload.data.run_state == "ready"
            resume["payload"]["force"] = True
            assert (await post(app, resume, path=PREFIX + "resume"))[0] == 422
            for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b"invalid"):
                assert (await post(app, raw=raw, path=PREFIX + "resume"))[0] == 422
            paths = app.openapi()["paths"]
            assert PREFIX + "resynchronizations/prepare" in paths and PREFIX + "resume" in paths

    asyncio.run(scenario())


def test_http_storage_failure_does_not_report_recovery_success(tmp_path):
    h = RecoveryHarness(tmp_path / "field-initialization")
    h.stop()
    candidate = h.prepare_recovery()
    request = h.resume_request(candidate).model_dump(mode="json")

    class Unavailable:
        def execute(self, request):
            raise sqlite3.OperationalError("injected persistence failure")

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            app.state.run_resumer = Unavailable()
            assert await post(app, request, path=PREFIX + "resume") == (
                503,
                {"code": "SESSION_STORE_UNAVAILABLE"},
            )

    asyncio.run(scenario())
    assert h.query().payload.data.run_state == "stopped"

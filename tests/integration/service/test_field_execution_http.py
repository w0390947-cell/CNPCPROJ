"""Execution HTTP messages share the strict field boundary and durable state."""

import asyncio
import sqlite3
from datetime import timedelta

from oilfield_energy.web_api import create_app
from tests.support.field_execution import Harness
from tests.support.field_http import post

PREFIX = "/api/field/single-microgrid/"


def test_http_dispatch_feedback_status_stop_and_validation(tmp_path):
    h = Harness(tmp_path / "field-initialization")
    assert h.prepare().payload.outcome == "accepted"

    async def scenario():
        app = create_app(tmp_path, field_transport=h.transport, field_clock=lambda: h.now)
        async with app.router.lifespan_context(app):
            request = h.dispatch_request().model_dump(mode="json")
            sent = await post(app, request, path=PREFIX + "control-cycles/dispatch")
            assert sent[0] == 202, sent
            assert await post(app, request, path=PREFIX + "control-cycles/dispatch") == sent
            h.observe()
            energy = dict(
                method="measured_energy",
                interval_start=h.now - timedelta(minutes=1),
                interval_end=h.now,
                source_ref="resource:storage:energy",
                energy_end_mwh=h.energy,
                uncertainty_mwh=0.0,
            )
            report = h.envelope(
                "ReportExecution",
                dict(
                    context_id=h.initial.payload.data.context_id,
                    cycle_id="cycle-0",
                    command_batch_id=h.prepared.payload.data.command_batch_id,
                    feedback_stage="cycle_end",
                    reported_at=h.now,
                    command_results=[r.model_dump(mode="json") for r in h.transport.results],
                    observed_snapshot_id=h.snapshot_id,
                    energy_evidence=energy,
                    reason="",
                ),
            )
            from oilfield_energy.workflows.field_initialization.contracts import (
                ReportExecutionRequest,
            )

            report = ReportExecutionRequest.model_validate(report).model_dump(mode="json")
            code, result = await post(app, report, path=PREFIX + "execution-reports")
            assert code == 200 and result["payload"]["data"]["cycle_status"] == "confirmed", result
            from oilfield_energy.workflows.field_initialization.contracts import (
                QueryRunStatusRequest,
                StopRunRequest,
            )

            query = QueryRunStatusRequest.model_validate(
                h.envelope("QueryRunStatus", {})
            ).model_dump(mode="json")
            assert (await post(app, query, path=PREFIX + "status"))[1]["payload"]["data"][
                "confirmed_snapshot_id"
            ] == h.snapshot_id
            stop = StopRunRequest.model_validate(
                h.envelope(
                    "StopRun",
                    dict(
                        reason="operator",
                        requested_at=h.now,
                        authority=h.document["payload"]["authority"],
                    ),
                )
            ).model_dump(mode="json")
            assert (await post(app, stop, path=PREFIX + "stop"))[1]["payload"]["data"][
                "run_state"
            ] == "stopped"
            for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b"not json"):
                assert (await post(app, raw=raw, path=PREFIX + "execution-reports"))[0] == 422
            report["payload"]["extra"] = True
            assert (await post(app, report, path=PREFIX + "execution-reports"))[0] == 422
            for path in ("execution-reports", "status", "stop", "control-cycles/dispatch"):
                assert PREFIX + path in app.openapi()["paths"]

    asyncio.run(scenario())


def test_http_execution_storage_failure_returns_503(tmp_path):
    h = Harness(tmp_path / "fixture")
    from oilfield_energy.workflows.field_initialization.contracts import QueryRunStatusRequest

    request = QueryRunStatusRequest.model_validate(h.envelope("QueryRunStatus", {})).model_dump(
        mode="json"
    )

    class Unavailable:
        def execute(self, request):
            raise sqlite3.OperationalError("disk failure")

    async def scenario():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            app.state.field_status_query = Unavailable()
            assert await post(app, request, path=PREFIX + "status") == (
                503,
                {"code": "SESSION_STORE_UNAVAILABLE"},
            )

    asyncio.run(scenario())

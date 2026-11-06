"""The public HTTP surface carries timed windows and independently measured feedback."""

import asyncio
from datetime import timedelta

from oilfield_energy.web_api import create_app
from oilfield_energy.workflows.field_initialization.contracts import ReportExecutionRequest
from tests.support.field_http import post
from tests.support.field_initialization import NOW
from tests.support.field_timing import TimedHarness


def test_http_bounded_dispatch_and_later_measured_confirmation(tmp_path):
    h = TimedHarness(tmp_path / "field-initialization")
    prepared = h.prepare()
    assert prepared.payload.data.time_basis == "bounded_monotone_v1"

    async def scenario():
        app = create_app(tmp_path, field_transport=h.transport, field_clock=lambda: h.now)
        async with app.router.lifespan_context(app):
            h.now += timedelta(seconds=1)
            request = h.dispatch_request().model_dump(mode="json")
            sent = await post(
                app, request, path="/api/field/single-microgrid/control-cycles/dispatch"
            )
            assert sent[0] == 202
            assert sent[1]["payload"]["data"]["validation_scope"] == "bounded_monotone_v1"
            assert (
                await post(app, request, path="/api/field/single-microgrid/control-cycles/dispatch")
                == sent
            )
            h.observe()
            report = ReportExecutionRequest.model_validate(
                h.envelope(
                    "ReportExecution",
                    dict(
                        context_id=h.initial.payload.data.context_id,
                        cycle_id="cycle-0",
                        command_batch_id=prepared.payload.data.command_batch_id,
                        feedback_stage="cycle_end",
                        reported_at=h.now,
                        command_results=h.transport.results,
                        observed_snapshot_id=h.snapshot_id,
                        energy_evidence=dict(
                            method="measured_energy",
                            interval_start=NOW,
                            interval_end=NOW + timedelta(minutes=1),
                            source_ref="resource:storage:energy",
                            energy_end_mwh=h.energy,
                            uncertainty_mwh=0.0,
                        ),
                        reason="",
                    ),
                )
            ).model_dump(mode="json")
            status, result = await post(
                app, report, path="/api/field/single-microgrid/execution-reports"
            )
            assert status == 200 and result["payload"]["data"]["cycle_status"] == "confirmed", (
                result
            )
            assert result["payload"]["data"]["confirmed_state"]["energy_mwh"] == h.energy
            schema = app.openapi()["components"]["schemas"]
            assert "BoundedTimingPolicy" in schema and "TimedCycleProof" in schema
            report["payload"]["allow_late_commands"] = True
            assert (await post(app, report, path="/api/field/single-microgrid/execution-reports"))[
                0
            ] == 422

    asyncio.run(scenario())

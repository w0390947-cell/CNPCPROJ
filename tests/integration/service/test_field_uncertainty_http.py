"""The normalized HTTP contract preserves error evidence and confirms state sets."""

import asyncio
from copy import deepcopy

from oilfield_energy.web_api import create_app
from oilfield_energy.workflows.field_initialization.contracts import (
    InitializeRequest,
    PrepareControlCycleRequest,
    PrepareControlCycleResponse,
)
from tests.support.field_cycles import cycle_document
from tests.support.field_http import post
from tests.support.field_timing import closed_loop_document
from tests.support.field_uncertainty import UncertainHarness


def test_http_uncertain_preparation_observation_confirmation_and_strict_schema(
    tmp_path,
):
    h = UncertainHarness(tmp_path / "field-initialization")

    async def scenario():
        app = create_app(
            tmp_path, field_transport=h.transport, field_clock=lambda: h.now
        )
        async with app.router.lifespan_context(app):
            raw = cycle_document(h.document, h.initial)
            raw["created_at"] = h.now
            request = PrepareControlCycleRequest.model_validate(raw).model_dump(
                mode="json"
            )
            code, body = await post(
                app, request, path="/api/field/single-microgrid/control-cycles/prepare"
            )
            assert code == 200, body
            assert body["payload"]["data"]["time_basis"] == "bounded_uncertain_v1"
            h.prepared = PrepareControlCycleResponse.model_validate(body)
            dispatch = h.dispatch_request().model_dump(mode="json")
            sent = await post(
                app,
                dispatch,
                path="/api/field/single-microgrid/control-cycles/dispatch",
            )
            assert sent[0] == 202, sent
            assert sent[1]["payload"]["data"]["confirmed_state"] is None
            assert (
                await post(
                    app,
                    dispatch,
                    path="/api/field/single-microgrid/control-cycles/dispatch",
                )
                == sent
            )
            snapshot = h.observe(submit=False).model_dump(mode="json")
            code, body = await post(
                app, snapshot, path="/api/field/single-microgrid/snapshots"
            )
            assert (
                code == 200
                and body["payload"]["data"]["state_semantics"] == "bounded_set"
            ), body
            report = h.report_request().model_dump(mode="json")
            code, body = await post(
                app, report, path="/api/field/single-microgrid/execution-reports"
            )
            assert code == 200, body
            data = body["payload"]["data"]
            assert (
                data["cycle_status"] == "confirmed" and data["confirmed_state"] is None
            )
            assert (
                data["confirmed_state_box"]["energy_mwh"]["low"]
                <= h.energy
                <= data["confirmed_state_box"]["energy_mwh"]["high"]
            )
            assert data["state_evidence_ref"].startswith(
                "physical-reachability-and-measurement:"
            )
            # Invalid uncertainty types fail wire validation before touching a session.
            init = InitializeRequest.model_validate(
                closed_loop_document(h.document)
            ).model_dump(mode="json")
            bad = deepcopy(init)
            bad["payload"]["initial_snapshot"]["device_states"][0]["p_mw"][
                "uncertainty"
            ]["absolute_error"] = True
            assert (await post(app, bad))[0] == 422
            bad = deepcopy(init)
            bad["payload"]["control_policy"]["observation_uncertainty"][
                "allow_force"
            ] = True
            assert (await post(app, bad))[0] == 422
            names = app.openapi()["components"]["schemas"]
            assert (
                "MeasurementUncertainty" in names
                and "ObservationUncertaintyPolicy" in names
            )
            assert len(h.transport.sent) == 1

    asyncio.run(scenario())

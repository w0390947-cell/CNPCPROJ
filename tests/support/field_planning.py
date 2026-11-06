"""Small analytic planning-update fixtures; never run the simulation loop."""

from copy import deepcopy
from datetime import timedelta

from oilfield_energy.bootstrap.field_initialization import create_field_initializer
from oilfield_energy.workflows.field_initialization.contracts import InitializeRequest
from tests.support.field_initialization import NOW, request_document


def initialized(root, document=None):
    original = document or request_document()
    request = InitializeRequest.model_validate(original)
    response = create_field_initializer(root, clock=lambda: NOW).execute(request)
    assert response.payload.outcome == "accepted", response
    return original, response


def update_document(original, initialized_response, *, offset_minutes=0):
    plan = deepcopy(original["payload"]["planning_inputs"])
    plan["context_id"] = initialized_response.payload.data.context_id
    plan["input_revision"] = "forecast-2"
    plan["issued_at"] = NOW.isoformat()
    plan["cost_rates"]["loss_cny_per_mwh"] = 20.0
    start = (NOW + timedelta(minutes=offset_minutes)).isoformat()
    plan["effective_from"] = start
    plan["forecast_points"][0]["at"] = start
    plan["tariff_intervals"][0]["start_at"] = start
    return dict(
        interface_version=initialized_response.interface_version,
        operation="UpdatePlanningInputs",
        message_id="update-1",
        correlation_id=None,
        microgrid_id=initialized_response.microgrid_id,
        run_id=initialized_response.run_id,
        created_at=NOW.isoformat(),
        payload=plan,
    )

"""Two-bus target fixture with explicit reactive capability; no simulation loop."""

from datetime import timedelta

from tests.support.field_initialization import NOW, request_document


def target_case():
    doc = request_document()
    wind = doc["payload"]["model_package"]["devices"][0]
    wind.update(q_min_mvar=-0.3, q_max_mvar=0.3, q_control_enabled=True)
    return doc


def target_document(initial):
    return dict(
        interface_version=initial.interface_version,
        operation="RequestPccTarget",
        message_id="target-message-1",
        correlation_id=None,
        microgrid_id=initial.microgrid_id,
        run_id=initial.run_id,
        created_at=NOW.isoformat(),
        payload=dict(
            context_id=initial.payload.data.context_id,
            target_id="external-1",
            target_p_import_mw=1.0,
            target_q_import_mvar=0.0,
            effective_from=NOW.isoformat(),
            response_deadline_at=(NOW + timedelta(minutes=2)).isoformat(),
            hold_until=(NOW + timedelta(minutes=5)).isoformat(),
            p_tolerance_mw=0.05,
            q_tolerance_mvar=0.05,
            source_ref="analytic-dispatch",
        ),
    )

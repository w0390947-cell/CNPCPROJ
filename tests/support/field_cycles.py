"""Single-minute shadow requests for analytic field fixtures."""

from copy import deepcopy
from datetime import timedelta

from tests.support.field_initialization import NOW


def cycle_document(original, initial):
    return dict(
        interface_version=initial.interface_version,
        operation="PrepareControlCycle",
        message_id="cycle-message-1",
        correlation_id=None,
        microgrid_id=initial.microgrid_id,
        run_id=initial.run_id,
        created_at=NOW.isoformat(),
        payload=dict(
            context_id=initial.payload.data.context_id,
            cycle_id="cycle-1",
            cycle_start_at=NOW.isoformat(),
            cycle_end_at=(NOW + timedelta(minutes=1)).isoformat(),
            snapshot_id=original["payload"]["initial_snapshot"]["snapshot_id"],
            authority=deepcopy(original["payload"]["authority"]),
        ),
    )

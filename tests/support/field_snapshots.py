"""Explicit observed snapshots from a two-bus analytic fixture."""

from copy import deepcopy
from datetime import timedelta

from tests.support.field_initialization import NOW


def snapshot_document(original, initialized, *, minute=0, sequence=2):
    snapshot = deepcopy(original["payload"]["initial_snapshot"])
    at = (NOW + timedelta(minutes=minute)).isoformat()

    def date(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in (
                    "snapshot_at",
                    "received_at",
                    "observed_at",
                    "status_observed_at",
                ):
                    value[key] = at
                else:
                    date(item)
        elif isinstance(value, list):
            for item in value:
                date(item)

    date(snapshot)
    snapshot.update(
        snapshot_id=f"observation-{sequence}",
        sequence=sequence,
        context_id=initialized.payload.data.context_id,
    )
    return dict(
        interface_version=initialized.interface_version,
        operation="SubmitSnapshot",
        message_id=f"snapshot-message-{sequence}",
        correlation_id=None,
        microgrid_id=initialized.microgrid_id,
        run_id=initialized.run_id,
        created_at=at,
        payload=snapshot,
    )

"""Immutable observation identity and source-clock ordering, independent of I/O."""

import json
from datetime import datetime, timezone
from hashlib import sha256

from .contracts_snapshots import ObservationCursor, PointWatermark, SubmittedSnapshot
from .contracts_values import MeasuredValue, SnapshotData, TimestampUncertainty
from .observation_uncertainty import window
from .validation import require


def snapshot_identity(snapshot: SubmittedSnapshot) -> str:
    ordered = snapshot.model_copy(
        update={
            "bus_loads": tuple(sorted(snapshot.bus_loads, key=lambda x: x.bus_id)),
            "device_states": tuple(
                sorted(snapshot.device_states, key=lambda x: x.resource_id)
            ),
            "renewable_availability": tuple(
                sorted(snapshot.renewable_availability, key=lambda x: x.resource_id)
            ),
            "held_setpoints": tuple(
                sorted(snapshot.held_setpoints, key=lambda x: x.resource_id)
            )
            if snapshot.held_setpoints is not None
            else None,
        }
    )

    def timestamp(value: object) -> str:
        if not isinstance(value, datetime):
            raise TypeError("Unexpected value in normalized snapshot")
        return value.astimezone(timezone.utc).isoformat()

    content = json.dumps(
        ordered.model_dump(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=timestamp,
    )
    return sha256(content.encode()).hexdigest()


def sample_windows(
    snapshot: SnapshotData,
) -> dict[tuple[str, str, str], tuple[datetime, datetime]]:
    """Do not let a previously invalid future sample poison the point watermark."""
    result: dict[tuple[str, str, str], tuple[datetime, datetime]] = {}

    def add(
        group: str,
        identity: str,
        field: str,
        at: datetime,
        v: MeasuredValue | None = None,
        clock: TimestampUncertainty | None = None,
    ) -> None:
        lo, hi = window(
            at,
            clock
            or (v.uncertainty.timestamp if v is not None and v.uncertainty else None),
        )
        if hi <= snapshot.received_at:
            result[group, identity, field] = lo, hi

    for b in snapshot.bus_loads:
        add("bus", b.bus_id, "p", b.p_mw.observed_at, b.p_mw)
        add("bus", b.bus_id, "q", b.q_mvar.observed_at, b.q_mvar)
    for d in snapshot.device_states:
        add("resource", d.resource_id, "p", d.p_mw.observed_at, d.p_mw)
        add("resource", d.resource_id, "q", d.q_mvar.observed_at, d.q_mvar)
        add(
            "resource",
            d.resource_id,
            "status",
            d.status_observed_at,
            clock=d.status_timestamp_uncertainty,
        )
        if d.energy_mwh is not None:
            add(
                "resource",
                d.resource_id,
                "energy",
                d.energy_mwh.observed_at,
                d.energy_mwh,
            )
    for g in snapshot.renewable_availability:
        add(
            "availability",
            g.resource_id,
            "p",
            g.available_p_mw.observed_at,
            g.available_p_mw,
        )
    for h in snapshot.held_setpoints or ():
        add(
            "resource",
            h.resource_id,
            "held_setpoint",
            h.observed_at,
            clock=h.timestamp_uncertainty,
        )
    for field, value in (
        ("p", snapshot.pcc.p_import_mw),
        ("q", snapshot.pcc.q_import_mvar),
        ("voltage", snapshot.pcc.voltage_pu),
    ):
        add("pcc", snapshot.pcc.bus_id, field, value.observed_at, value)
    return result


def sample_times(snapshot: SnapshotData) -> dict[tuple[str, str, str], datetime]:
    return {k: v[0] for k, v in sample_windows(snapshot).items()}


def next_observation_cursor(
    snapshot: SubmittedSnapshot,
    initial: SnapshotData,
    previous: ObservationCursor | None,
) -> ObservationCursor:
    before_snapshot = previous.snapshot if previous else initial
    require(
        snapshot.sequence > before_snapshot.sequence
        and snapshot.snapshot_at >= before_snapshot.snapshot_at,
        "SNAPSHOT_OUT_OF_ORDER",
        "Sequence must increase and boundary cannot move backwards",
    )
    before = (
        {
            (x.group, x.entity_id, x.field): (
                x.observed_at,
                x.observed_until or x.observed_at,
            )
            for x in previous.watermarks
        }
        if previous
        else sample_windows(initial)
    )
    known = sample_windows(initial)
    after = {key: at for key, at in sample_windows(snapshot).items() if key in known}
    require(
        all(after[key][0] >= at[1] for key, at in before.items() if key in after),
        "SNAPSHOT_OUT_OF_ORDER",
        "Original per-point sample times moved backwards",
    )
    # Missing points in an invalid snapshot must not erase earlier watermarks.
    before.update(after)
    return ObservationCursor(
        snapshot=snapshot,
        watermarks=tuple(
            PointWatermark(
                group=key[0],
                entity_id=key[1],
                field=key[2],
                observed_at=at[0],
                observed_until=at[1],
            )
            for key, at in sorted(before.items())
        ),
    )

"""Identity, quality, timing and metering checks on normalized field inputs."""

from collections.abc import Hashable, Iterable
from datetime import datetime
from typing import TypeVar

from .contracts import (
    ControlPolicy,
    InitializationFailure,
    InitializeRequest,
    ModelPackage,
    PointMapping,
)
from .contracts_values import (
    Authority,
    MeasuredValue,
    SnapshotData,
    TimestampUncertainty,
)
from .observation_uncertainty import (
    observe_bounded,
    validate_clock,
    validate_observation_evidence,
    window,
)


def require(ok: bool, code: str, message: str, path: str | None = None) -> None:
    if not ok:
        raise InitializationFailure(code, message, path)


Identity = TypeVar("Identity", bound=Hashable)


def unique(values: Iterable[Identity], label: str) -> set[Identity]:
    items = tuple(values)
    require(len(items) == len(set(items)), "IDENTITY_MISMATCH", f"Duplicate {label}")
    return set(items)


def validate_snapshot(
    request: InitializeRequest,
    buses: tuple[str, ...],
    pcc_bus: str,
    mapping: tuple[PointMapping, ...],
    now: datetime,
) -> None:
    p = request.payload
    require(
        p.mode == "shadow" or p.authority.write_enabled,
        "CLOSED_LOOP_UNAVAILABLE",
        "Closed-loop initialization requires write authority and an injected transport admission gate",
    )
    validate_snapshot_values(
        p.initial_snapshot,
        p.model_package,
        p.control_policy,
        p.authority,
        buses,
        pcc_bus,
        mapping,
        now,
        request.created_at,
    )


def validate_snapshot_values(
    s: SnapshotData,
    model: ModelPackage,
    policy: ControlPolicy,
    authority: Authority,
    buses: tuple[str, ...],
    pcc_bus: str,
    mapping: tuple[PointMapping, ...],
    now: datetime,
    created_at: datetime,
) -> None:
    """Validate normalized observations without promoting a completed state."""
    timing = policy.bounded_timing
    uncertain = policy.observation_uncertainty
    if uncertain is None:
        require(
            all(
                h.timestamp_uncertainty is None and h.since_uncertainty is None
                for h in s.held_setpoints or ()
            ),
            "UNCERTAINTY_NOT_ADMITTED",
            "An exact profile cannot ignore held-record clock uncertainty",
        )
    if uncertain:
        validate_observation_evidence(s, policy, now)
    if timing:
        require(
            (s.received_at - s.snapshot_at).total_seconds()
            <= timing.snapshot_delivery_max_seconds,
            "TELEMETRY_INVALID",
            "Snapshot delivery exceeded the admitted timed bound",
        )
        held = s.held_setpoints
        require(
            held is not None
            and len(held) == len(s.device_states)
            and len({h.resource_id for h in held}) == len(held),
            "HOLD_EVIDENCE_REQUIRED",
            "Timed boundaries require complete unique held-setpoint evidence",
        )
        assert held is not None
        states = {d.resource_id: d for d in s.device_states}
        for h in held:
            require(
                h.resource_id in states,
                "IDENTITY_MISMATCH",
                "Held setpoint names an unknown resource",
            )
            state = states[h.resource_id]
            require(
                (uncertain is not None or h.observed_at == s.snapshot_at)
                and (
                    uncertain is not None
                    or (
                        h.p_mw == state.p_mw.value
                        and h.q_mvar == state.q_mvar.value
                        and state.p_mw.observed_at == s.snapshot_at
                        and state.q_mvar.observed_at == s.snapshot_at
                        and (
                            state.energy_mwh is None
                            or state.energy_mwh.observed_at == s.snapshot_at
                        )
                    )
                ),
                "TIMED_BOUNDARY_UNSETTLED",
                "Timed boundaries need exact settled measured state and held setpoints",
            )
            if uncertain:
                require(
                    h.held_since is not None,
                    "HOLD_EVIDENCE_REQUIRED",
                    "Hold adoption time is required",
                )
                assert h.held_since is not None
                validate_clock(h.since_uncertainty, h.held_since, policy, now)
                lo, hi = validate_clock(
                    h.timestamp_uncertainty, h.observed_at, policy, now
                )
                require(
                    hi <= s.received_at
                    and (now - lo).total_seconds() <= policy.telemetry_max_age_seconds,
                    "TELEMETRY_INVALID",
                    "Held-setpoint record is future-dated or stale",
                )
        resources = {r.resource_id: r for r in timing.resources}
        require(
            set(resources) == {d.resource_id for d in model.devices},
            "IDENTITY_MISMATCH",
            "Timing evidence must identify every device",
        )
        require(
            all(
                resources[d.resource_id].settling_max_seconds == 0
                for d in model.devices
                if d.kind == "storage"
            ),
            "TIMED_RESPONSE_UNPROVEN",
            "Storage P requires the declared instantaneous ZOH response",
        )
    require(
        s.snapshot_at.second == 0 and s.snapshot_at.microsecond == 0,
        "INVALID_MESSAGE",
        "snapshot_at must be a minute boundary",
    )
    require(
        0 <= (now - created_at).total_seconds() <= policy.telemetry_max_age_seconds,
        "TELEMETRY_INVALID",
        "Request creation time is stale or future",
    )
    require(
        s.snapshot_at <= s.received_at <= now,
        "TELEMETRY_INVALID",
        "Invalid snapshot/receive/decision ordering",
    )
    require(
        authority.valid_until > now,
        "AUTHORITY_LOST",
        "Declared authority has expired",
    )
    require(
        (
            s.model_revision,
            s.mapping_revision,
            s.operating_mode_id,
            s.permission_revision,
        )
        == (
            model.model_revision,
            model.mapping_revision,
            model.operating_mode_id,
            authority.permission_revision,
        ),
        "CONTEXT_CHANGED",
        "Snapshot versions differ from initialized model or authority",
    )
    bus_set = set(buses)
    devices = {d.resource_id: d for d in model.devices}
    require(
        len(devices) == len(model.devices), "IDENTITY_MISMATCH", "Duplicate devices"
    )
    require(
        all(d.bus_id in bus_set for d in model.devices),
        "IDENTITY_MISMATCH",
        "Unknown device bus",
    )
    require(
        sum(d.kind == "storage" for d in model.devices) == 1,
        "MODEL_UNSUPPORTED",
        "Exactly one storage model is required",
    )
    require(
        unique((x.bus_id for x in s.bus_loads), "load buses") == bus_set,
        "IDENTITY_MISMATCH",
        "Snapshot must cover every bus exactly once",
    )
    require(
        unique((x.resource_id for x in s.device_states), "states") == set(devices),
        "IDENTITY_MISMATCH",
        "Snapshot must cover every resource exactly once",
    )
    renewable = {d.resource_id for d in model.devices if d.kind in ("wind", "pv")}
    require(
        unique((x.resource_id for x in s.renewable_availability), "availability")
        == renewable,
        "IDENTITY_MISMATCH",
        "Availability identities differ",
    )
    require(s.pcc.bus_id == pcc_bus, "IDENTITY_MISMATCH", "PCC bus differs")
    scopes = {x.bus_id: x for x in model.load_scopes}
    require(
        len(scopes) == len(model.load_scopes) and set(scopes) == bus_set,
        "METERING_SCOPE_UNCONFIRMED",
        "Gross-load scopes must cover all buses",
    )
    unique((x.scope_ref for x in model.load_scopes), "load scope IDs")
    unique(
        (member for x in model.load_scopes for member in x.members),
        "load metering members",
    )
    point_ids = unique((x.mapping_id for x in mapping), "mapping IDs")
    unique((x.point_id for x in mapping), "point IDs")
    unique(
        ((x.entity_kind, x.entity_id, x.quantity, x.access) for x in mapping),
        "mapping targets",
    )
    points = {x.mapping_id: x for x in mapping}
    for x in mapping:
        allowed = (
            bus_set
            if x.entity_kind == "bus_load"
            else set(devices)
            if x.entity_kind == "resource"
            else {pcc_bus}
            if x.entity_kind == "pcc"
            else {s.operating_state_ref}
        )
        require(
            x.entity_id in allowed,
            "IDENTITY_MISMATCH",
            f"Unknown point entity: {x.mapping_id}",
        )
        require(
            (x.access == "write") == (x.quantity in ("p_setpoint", "q_setpoint")),
            "INVALID_MESSAGE",
            f"Access/quantity conflict: {x.mapping_id}",
        )
        unit = {
            "p": "MW",
            "p_setpoint": "MW",
            "q": "Mvar",
            "q_setpoint": "Mvar",
            "energy": "MWh",
            "voltage": "pu",
        }.get(x.quantity, "1")
        require(
            x.normalized_unit == unit,
            "INVALID_MESSAGE",
            f"Wrong normalized unit: {x.mapping_id}",
        )
        if x.access == "write":
            require(
                x.entity_kind == "resource",
                "INVALID_MESSAGE",
                "Only resource setpoints may be declared",
            )
            device = devices[x.entity_id]
            require(
                device.p_control_enabled
                if x.quantity == "p_setpoint"
                else device.q_control_enabled,
                "AUTHORITY_LOST",
                "Write point declared for an unauthorized axis",
            )
    times: list[datetime] = []

    def timestamp(
        at: datetime,
        v: MeasuredValue | None = None,
        clock: TimestampUncertainty | None = None,
    ) -> None:
        lo, hi = window(
            at,
            clock
            or (v.uncertainty.timestamp if v is not None and v.uncertainty else None),
        )
        age = (now - lo).total_seconds()
        require(
            hi <= now and age <= policy.telemetry_max_age_seconds
            if uncertain
            else -policy.future_clock_skew_seconds
            <= age
            <= policy.telemetry_max_age_seconds,
            "TELEMETRY_INVALID",
            "Measurement timestamp is stale or in the future",
        )
        require(
            max(
                abs((lo - s.snapshot_at).total_seconds()),
                abs((hi - s.snapshot_at).total_seconds()),
            )
            <= policy.snapshot_max_skew_seconds,
            "TELEMETRY_INVALID",
            "Measurement does not align with snapshot",
        )
        require(
            hi <= s.received_at,
            "TELEMETRY_INVALID",
            "Observation-time range follows receipt",
        )
        times.extend((lo, hi))

    def measure(
        v: MeasuredValue,
        kind: str,
        identity: str,
        quantity: str,
        *,
        available: bool = False,
    ) -> float:
        require(
            uncertain is not None or v.uncertainty is None,
            "UNCERTAINTY_NOT_ADMITTED",
            "An exact profile cannot silently ignore uncertainty evidence",
        )
        timestamp(v.observed_at, v)
        require(
            v.value is not None
            and v.quality == "good"
            and v.value_semantics == "instantaneous",
            "TELEMETRY_INVALID",
            f"Invalid instantaneous {identity}/{quantity}",
        )
        require(
            v.source_kind == "measured" or available or uncertain is not None,
            "MODEL_UNSUPPORTED",
            "Estimated state uncertainty is not supported in initialization v1",
        )
        if not available:
            require(
                v.source_ref in point_ids,
                "IDENTITY_MISMATCH",
                f"Unknown source_ref {v.source_ref}",
            )
            point = points[v.source_ref]
            require(
                (point.entity_kind, point.entity_id, point.quantity, point.access)
                == (kind, identity, quantity, "read"),
                "IDENTITY_MISMATCH",
                f"Point source mismatch: {v.source_ref}",
            )
        assert v.value is not None
        return v.value

    for load in s.bus_loads:
        require(
            load.scope_ref == scopes[load.bus_id].scope_ref,
            "METERING_SCOPE_UNCONFIRMED",
            "Load scope differs",
        )
        require(
            measure(load.p_mw, "bus_load", load.bus_id, "p") >= 0
            or uncertain is not None,
            "INVALID_MESSAGE",
            "Gross P load must be nonnegative",
        )
        measure(load.q_mvar, "bus_load", load.bus_id, "q")
        require(
            all(
                points[v.source_ref].scope_ref == load.scope_ref
                for v in (load.p_mw, load.q_mvar)
            ),
            "METERING_SCOPE_UNCONFIRMED",
            "Point and load scopes differ",
        )
    for state in s.device_states:
        require(
            uncertain is not None or state.status_timestamp_uncertainty is None,
            "UNCERTAINTY_NOT_ADMITTED",
            "Exact status cannot ignore clock uncertainty",
        )
        timestamp(state.status_observed_at, clock=state.status_timestamp_uncertainty)
        require(
            state.online and state.remote_enabled and not state.blocked,
            "AUTHORITY_LOST",
            f"Resource unavailable: {state.resource_id}",
        )
        measure(state.p_mw, "resource", state.resource_id, "p")
        measure(state.q_mvar, "resource", state.resource_id, "q")
        if devices[state.resource_id].kind == "storage":
            require(
                state.energy_mwh is not None,
                "TELEMETRY_INVALID",
                "Storage energy is missing",
            )
            assert state.energy_mwh is not None
            measure(state.energy_mwh, "resource", state.resource_id, "energy")
        else:
            require(
                state.energy_mwh is None,
                "INVALID_MESSAGE",
                "Energy belongs only to storage",
            )
    for item in s.renewable_availability:
        require(
            item.method_ref == policy.availability_method_ref,
            "INVALID_MESSAGE",
            "Availability method differs",
        )
        available = measure(
            item.available_p_mw, "resource", item.resource_id, "p", available=True
        )
        require(
            0 <= available <= devices[item.resource_id].p_max_mw
            or uncertain is not None,
            "INVALID_MESSAGE",
            "Availability exceeds nameplate or is negative",
        )
    measure(s.pcc.p_import_mw, "pcc", pcc_bus, "p")
    measure(s.pcc.q_import_mvar, "pcc", pcc_bus, "q")
    require(
        measure(s.pcc.voltage_pu, "pcc", pcc_bus, "voltage") == 1.0
        or uncertain is not None,
        "MODEL_UNSUPPORTED",
        "Current single model fixes the root voltage at 1.0 pu",
    )
    require(
        (max(times) - min(times)).total_seconds() <= policy.snapshot_max_skew_seconds,
        "TELEMETRY_INVALID",
        "Snapshot inter-point skew exceeds policy",
    )
    if uncertain:
        observe_bounded(s, model, policy, tuple(d.resource_id for d in model.devices))

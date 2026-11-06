"""Calibrated set-membership observations on the trusted service minute clock.

Only measurement, held-response and already verified physical reachability may
contract a state set. Safety limits and target/task bands are never estimators.
"""

from datetime import datetime, timedelta
from fractions import Fraction as F

from oilfield_energy.kernel.enclosures import lower, upper
from oilfield_energy.kernel.intervals import Interval
from oilfield_energy.modules.control.contracts import TubeBox
from oilfield_energy.modules.power_flow.contracts import PowerInterval
from oilfield_energy.workflows.single_execution.contracts import (
    BoundedBusLoad,
    FirmGeneration,
    SingleForecastEnvelope,
)

from .contracts_model import ControlPolicy, ModelPackage
from .contracts_timing import ResourceTiming
from .contracts_values import (
    Bounds,
    InitializationFailure,
    MeasuredValue,
    SnapshotData,
    TimestampUncertainty,
    Value,
)


def check(ok: bool, code: str, message: str) -> None:
    if not ok:
        raise InitializationFailure(code, message)


def window(
    at: datetime, evidence: TimestampUncertainty | None
) -> tuple[datetime, datetime]:
    width = (
        timedelta(microseconds=round(evidence.error_seconds * 1_000_000))
        if evidence
        else timedelta(0)
    )
    return at - width, at + width


def enclosure(low: F, high: F) -> Interval:
    return Interval(low=lower(low), high=upper(high))


def intersect(first: Interval, second: Interval) -> Interval:
    lo, hi = max(first.low, second.low), min(first.high, second.high)
    check(
        lo <= hi,
        "OBSERVATION_INCONSISTENT",
        "Independent measurement and physical evidence have an empty intersection",
    )
    return Interval(low=lo, high=hi)


def intersect_boxes(first: TubeBox, second: TubeBox) -> TubeBox:
    check(
        len(first.p_mw) == len(second.p_mw) and len(first.q_mvar) == len(second.q_mvar),
        "IDENTITY_MISMATCH",
        "State-set resource axes differ",
    )
    return TubeBox(
        p_mw=tuple(
            intersect(a, b) for a, b in zip(first.p_mw, second.p_mw, strict=True)
        ),
        q_mvar=tuple(
            intersect(a, b) for a, b in zip(first.q_mvar, second.q_mvar, strict=True)
        ),
        energy_mwh=intersect(first.energy_mwh, second.energy_mwh),
    )


def contains_box(outer: TubeBox, inner: TubeBox) -> bool:
    return (
        len(outer.p_mw) == len(inner.p_mw)
        and len(outer.q_mvar) == len(inner.q_mvar)
        and all(
            a.low <= b.low <= b.high <= a.high
            for a, b in zip(
                (*outer.p_mw, *outer.q_mvar, outer.energy_mwh),
                (*inner.p_mw, *inner.q_mvar, inner.energy_mwh),
                strict=True,
            )
        )
    )


def settled_range(setpoint: float, error: float, exact_zero: bool) -> Interval:
    if setpoint == 0 and exact_zero:
        error = 0.0
    return enclosure(F(setpoint) - F(error), F(setpoint) + F(error))


def resource_settled(
    timing: ResourceTiming, p: float, q: float
) -> tuple[Interval, Interval]:
    return (
        settled_range(p, timing.settled_p_error_mw, timing.zero_p_is_exact),
        settled_range(q, timing.settled_q_error_mvar, timing.zero_q_is_exact),
    )


def measurements(s: SnapshotData) -> tuple[MeasuredValue, ...]:
    return (
        tuple(v for b in s.bus_loads for v in (b.p_mw, b.q_mvar))
        + tuple(
            v
            for d in s.device_states
            for v in (d.p_mw, d.q_mvar, d.energy_mwh)
            if v is not None
        )
        + tuple(g.available_p_mw for g in s.renewable_availability)
        + (s.pcc.p_import_mw, s.pcc.q_import_mvar, s.pcc.voltage_pu)
    )


def validate_clock(
    evidence: TimestampUncertainty | None,
    at: datetime,
    policy: ControlPolicy,
    now: datetime,
    expected_error: float | None = None,
) -> tuple[datetime, datetime]:
    uncertainty, timing = policy.observation_uncertainty, policy.bounded_timing
    assert uncertainty is not None and timing is not None
    check(
        evidence is not None,
        "CLOCK_EVIDENCE_REQUIRED",
        "Remote event time needs clock evidence",
    )
    assert evidence is not None
    lo, hi = window(at, evidence)
    check(
        evidence.clock_revision == uncertainty.clock_revision
        and evidence.error_seconds
        == (
            timing.clock_error_bound_seconds
            if expected_error is None
            else expected_error
        )
        and evidence.error_seconds <= timing.clock_error_bound_seconds,
        "CLOCK_EVIDENCE_INVALID",
        "Clock revision or uncertainty exceeds the admitted profile",
    )
    check(
        evidence.valid_from <= lo <= hi < evidence.valid_until
        and now < evidence.valid_until,
        "CLOCK_EVIDENCE_INVALID",
        "Clock evidence does not cover the event or decision",
    )
    return lo, hi


def validate_observation_evidence(
    s: SnapshotData, policy: ControlPolicy, now: datetime
) -> None:
    uncertainty = policy.observation_uncertainty
    assert uncertainty is not None
    check(
        uncertainty.valid_from <= s.snapshot_at <= now < uncertainty.valid_until,
        "UNCERTAINTY_POLICY_EXPIRED",
        "Metrology policy is not valid at the boundary or decision",
    )
    for state in s.device_states:
        lo, hi = validate_clock(
            state.status_timestamp_uncertainty, state.status_observed_at, policy, now
        )
        check(
            max(
                abs((lo - s.snapshot_at).total_seconds()),
                abs((hi - s.snapshot_at).total_seconds()),
            )
            <= uncertainty.boundary_radius_seconds,
            "SAMPLE_TIME_UNRESOLVED",
            "Device status-time range exceeds the boundary guard",
        )
    values = measurements(s)
    refs = [v.source_ref for v in values]
    limits = {m.source_ref: m for m in uncertainty.measurements}
    check(
        len(set(refs)) == len(refs) and set(refs) == set(limits),
        "UNCERTAINTY_SOURCE_MISMATCH",
        "Metrology limits must cover each observation source exactly once",
    )
    for value in values:
        error = value.uncertainty
        check(
            error is not None,
            "MEASUREMENT_EVIDENCE_REQUIRED",
            "Every scalar needs explicit calibration and clock evidence",
        )
        assert error is not None
        limit = limits[value.source_ref]
        lo, hi = validate_clock(
            error.timestamp,
            value.observed_at,
            policy,
            now,
            expected_error=limit.max_timestamp_error_seconds,
        )
        check(
            error.calibration_revision == limit.calibration_revision
            and value.source_kind == limit.source_kind
            and error.absolute_error == limit.max_absolute_error
            and error.timestamp.error_seconds == limit.max_timestamp_error_seconds,
            "MEASUREMENT_EVIDENCE_INVALID",
            "Calibration revision or measurement/time error exceeds its admitted limit",
        )
        check(
            error.valid_from <= lo <= hi < error.valid_until
            and now < error.valid_until,
            "MEASUREMENT_EVIDENCE_INVALID",
            "Calibration does not cover the sample or decision",
        )
        check(
            max(
                abs((lo - s.snapshot_at).total_seconds()),
                abs((hi - s.snapshot_at).total_seconds()),
            )
            <= uncertainty.boundary_radius_seconds,
            "SAMPLE_TIME_UNRESOLVED",
            "Sample-time range exceeds the stable boundary-alignment guard",
        )


class BoundedObservation(Value):
    state_box: TubeBox
    inputs: SingleForecastEnvelope
    measurement_ranges: dict[str, Bounds]
    aligned_ranges: dict[str, Bounds]


def observe_bounded(
    s: SnapshotData,
    model: ModelPackage,
    policy: ControlPolicy,
    resource_ids: tuple[str, ...],
) -> BoundedObservation:
    uncertainty, timing = policy.observation_uncertainty, policy.bounded_timing
    assert uncertainty is not None and timing is not None
    limits = {m.source_ref: m for m in uncertainty.measurements}
    raw: dict[str, Bounds] = {}
    aligned: dict[str, Bounds] = {}

    def read(v: MeasuredValue, *, energy_rate: F | None = None) -> Interval:
        assert v.value is not None and v.uncertainty is not None
        error = v.uncertainty
        r = enclosure(
            F(v.value) - F(error.absolute_error), F(v.value) + F(error.absolute_error)
        )
        raw[v.source_ref] = Bounds(low=r.low, high=r.high)
        lo, hi = window(v.observed_at, error.timestamp)
        low_dt = F((s.snapshot_at - hi) // timedelta(microseconds=1), 1_000_000)
        high_dt = F((s.snapshot_at - lo) // timedelta(microseconds=1), 1_000_000)
        if energy_rate is not None:
            a, b = energy_rate * low_dt, energy_rate * high_dt
            r = enclosure(F(r.low) + min(a, b), F(r.high) + max(a, b))
        else:
            drift = F(limits[v.source_ref].alignment_rate_per_second) * max(
                abs(low_dt), abs(high_dt)
            )
            r = enclosure(F(r.low) - drift, F(r.high) + drift)
        aligned[v.source_ref] = Bounds(low=r.low, high=r.high)
        return r

    held = {h.resource_id: h for h in s.held_setpoints or ()}
    states = {d.resource_id: d for d in s.device_states}
    devices = {d.resource_id: d for d in model.devices}
    response = {r.resource_id: r for r in timing.resources}
    ps: list[Interval] = []
    qs: list[Interval] = []
    energy: Interval | None = None
    for identity in resource_ids:
        h, d, r = held[identity], states[identity], response[identity]
        check(
            h.held_since is not None and h.since_uncertainty is not None,
            "HOLD_EVIDENCE_REQUIRED",
            "Held setpoint needs its original adoption time and clock evidence",
        )
        assert h.held_since is not None and h.since_uncertainty is not None
        since_upper = window(h.held_since, h.since_uncertainty)[1]
        earliest = min(
            window(v.observed_at, v.uncertainty.timestamp if v.uncertainty else None)[0]
            for v in (d.p_mw, d.q_mvar, d.energy_mwh)
            if v is not None
        )
        latest = max(
            window(v.observed_at, v.uncertainty.timestamp if v.uncertainty else None)[1]
            for v in (d.p_mw, d.q_mvar, d.energy_mwh)
            if v is not None
        )
        check(
            since_upper + timedelta(seconds=r.settling_max_seconds)
            <= min(s.snapshot_at, earliest),
            "TIMED_BOUNDARY_UNSETTLED",
            "Held-response settling is not proven before the full sample window",
        )
        check(
            h.timestamp_uncertainty is not None
            and window(h.observed_at, h.timestamp_uncertainty)[0]
            >= max(s.snapshot_at, latest),
            "HOLD_EVIDENCE_INCOMPLETE",
            "Continuous held-setpoint history does not cover the full sample-time range",
        )
        physical_p, physical_q = resource_settled(r, h.p_mw, h.q_mvar)
        ps.append(intersect(read(d.p_mw), physical_p))
        qs.append(intersect(read(d.q_mvar), physical_q))
        device = devices[identity]
        if device.kind == "storage":
            check(
                r.settled_p_error_mw == 0 and r.settling_max_seconds == 0,
                "MODEL_UNSUPPORTED",
                "Storage active power remains exact instantaneous ZOH",
            )
            assert (
                device.eta_charge is not None
                and device.eta_discharge is not None
                and d.energy_mwh is not None
            )
            p = F(h.p_mw)
            rate = (
                -p / F(device.eta_discharge) if p >= 0 else -p * F(device.eta_charge)
            ) / 3600
            check(
                F(limits[d.energy_mwh.source_ref].alignment_rate_per_second)
                >= abs(rate),
                "OBSERVATION_INCONSISTENT",
                "Energy alignment-rate limit contradicts the known held PCS power",
            )
            energy = read(d.energy_mwh, energy_rate=rate)
    assert energy is not None
    loads: list[BoundedBusLoad] = []
    for b in s.bus_loads:
        p, q = read(b.p_mw), read(b.q_mvar)
        check(p.high >= 0, "OBSERVATION_INCONSISTENT", "Gross load cannot be negative")
        loads.append(
            BoundedBusLoad(
                bus=b.bus_id,
                p_mw=PowerInterval(low=max(0.0, p.low), high=p.high),
                q_mvar=PowerInterval(low=q.low, high=q.high),
            )
        )
    generation: list[FirmGeneration] = []
    for g in s.renewable_availability:
        available = read(g.available_p_mw)
        check(
            available.high >= 0 and available.low <= devices[g.resource_id].p_max_mw,
            "OBSERVATION_INCONSISTENT",
            "Availability contradicts the resource's physical range",
        )
        generation.append(
            FirmGeneration(
                resource_id=g.resource_id, minimum_available_mw=max(0.0, available.low)
            )
        )
    for v in (s.pcc.p_import_mw, s.pcc.q_import_mvar, s.pcc.voltage_pu):
        read(v)
    root = aligned[s.pcc.voltage_pu.source_ref]
    allowed = uncertainty.root_voltage_bounds_pu
    check(
        allowed.low <= root.low <= root.high <= allowed.high,
        "OBSERVATION_OUTSIDE_ENVELOPE",
        "The full aligned root-voltage range must stay inside the admitted source envelope",
    )
    return BoundedObservation(
        state_box=TubeBox(p_mw=tuple(ps), q_mvar=tuple(qs), energy_mwh=energy),
        inputs=SingleForecastEnvelope(loads=tuple(loads), generation=tuple(generation)),
        measurement_ranges=raw,
        aligned_ranges=aligned,
    )


def validate_bounded_forecast(
    expected: SingleForecastEnvelope, actual: SingleForecastEnvelope
) -> None:
    loads = {b.bus: b for b in actual.loads}
    check(
        len(loads) == len(actual.loads)
        and set(loads) == {b.bus for b in expected.loads},
        "IDENTITY_MISMATCH",
        "Bounded load axes differ",
    )
    for bound in expected.loads:
        value = loads[bound.bus]
        check(
            bound.p_mw.low <= value.p_mw.low <= value.p_mw.high <= bound.p_mw.high
            and bound.q_mvar.low
            <= value.q_mvar.low
            <= value.q_mvar.high
            <= bound.q_mvar.high,
            "OBSERVATION_OUTSIDE_ENVELOPE",
            "The full aligned load range must stay inside the declared forecast",
        )
    generation = {g.resource_id: g.minimum_available_mw for g in actual.generation}
    check(
        len(generation) == len(actual.generation)
        and set(generation) == {g.resource_id for g in expected.generation},
        "IDENTITY_MISMATCH",
        "Bounded availability axes differ",
    )
    check(
        all(
            generation[g.resource_id] >= g.minimum_available_mw
            for g in expected.generation
        ),
        "OBSERVATION_OUTSIDE_ENVELOPE",
        "Available-generation lower bounds no longer support the declared forecast",
    )

"""Whole-set observation safety and tracking, without advancing confirmed state."""

from fractions import Fraction as F

from oilfield_energy.modules.control.api import validate_device_box
from oilfield_energy.workflows.single_execution.api import (
    device_model_at,
    unified_network_box,
)

from .contracts import (
    InitializationEvidence,
    InterfaceError,
    PccTargetEvidence,
    SnapshotAssessment,
    SubmittedSnapshot,
)
from .observation_uncertainty import observe_bounded, validate_bounded_forecast
from .timing_certification import midpoint


def assess_uncertain(
    current: InitializationEvidence,
    snapshot: SubmittedSnapshot,
    target: PccTargetEvidence | None,
    step: int,
) -> tuple[SnapshotAssessment, tuple[InterfaceError, ...]]:
    policy = current.request.payload.control_policy
    values = observe_bounded(
        snapshot,
        current.resolved_model_package,
        policy,
        tuple(r.resource_id for r in current.domain.devices.resources),
    )
    box = values.state_box
    evidence = SnapshotAssessment(
        context_id=current.domain.context_id,
        step=step,
        observed_state=midpoint(box),
        observed_box=box,
        observed_inputs=values.inputs,
        measurement_ranges=values.measurement_ranges,
        aligned_ranges=values.aligned_ranges,
        state_semantics="bounded_set",
    )
    errors: list[InterfaceError] = []
    for code, operation in (
        (
            "OBSERVATION_OUTSIDE_ENVELOPE",
            lambda: validate_bounded_forecast(
                current.domain.forecasts[step], values.inputs
            ),
        ),
        (
            "STATE_OUTSIDE_CERTIFICATE",
            lambda: validate_device_box(device_model_at(current.domain, step), box),
        ),
    ):
        try:
            operation()
        except (ValueError, ArithmeticError) as exc:
            errors.append(InterfaceError(code=code, message=str(exc)))
    try:
        network = unified_network_box(current.domain, box, values.inputs)
        evidence = evidence.model_copy(update={"network": network})
        if not network.certified:
            errors.append(
                InterfaceError(code="SNAPSHOT_SAFETY_UNPROVEN", message=network.reason)
            )
        else:
            for v, bound, tolerance in (
                (
                    snapshot.pcc.p_import_mw,
                    network.pcc_p_mw,
                    policy.pcc_model_tolerance_mw,
                ),
                (
                    snapshot.pcc.q_import_mvar,
                    network.pcc_q_mvar,
                    policy.pcc_model_tolerance_mvar,
                ),
            ):
                measured = values.aligned_ranges[v.source_ref]
                assert bound is not None
                # Consistency only; device/network safety above covers the whole state set.
                if F(measured.high) < F(bound.low) - F(tolerance) or F(
                    measured.low
                ) > F(bound.high) + F(tolerance):
                    errors.append(
                        InterfaceError(
                            code="OBSERVATION_INCONSISTENT",
                            message="PCC observation and modeled injection ranges are disjoint",
                        )
                    )
    except (ValueError, ArithmeticError) as exc:
        errors.append(InterfaceError(code="SNAPSHOT_SAFETY_UNPROVEN", message=str(exc)))
    if target and target.context_id == current.domain.context_id:
        t = target.request.payload
        if t.response_deadline_at <= snapshot.snapshot_at < target.guaranteed_until:
            for v, goal, tolerance in (
                (snapshot.pcc.p_import_mw, t.target_p_import_mw, t.p_tolerance_mw),
                (
                    snapshot.pcc.q_import_mvar,
                    t.target_q_import_mvar,
                    t.q_tolerance_mvar,
                ),
            ):
                actual = values.aligned_ranges[v.source_ref]
                lo, hi = F(goal) - F(tolerance), F(goal) + F(tolerance)
                if not lo <= F(actual.low) <= F(actual.high) <= hi:
                    disjoint = F(actual.high) < lo or F(actual.low) > hi
                    errors.append(
                        InterfaceError(
                            code="TARGET_TRACKING_VIOLATED"
                            if disjoint
                            else "TARGET_TRACKING_UNPROVEN",
                            message="The full PCC observation range does not prove tracking inside the original band",
                        )
                    )
    return evidence, tuple(errors)

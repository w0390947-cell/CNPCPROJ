"""Fixed observed-state assessment; no optimization, simulation or state advance."""

from oilfield_energy.modules.control.api import (
    point_box,
    tube_contains,
    validate_device_box,
)
from oilfield_energy.modules.control.contracts import TubeState
from oilfield_energy.modules.power_flow.contracts import PowerInterval as I
from oilfield_energy.workflows.single_execution.api import (
    device_model_at,
    unified_network_box,
    validate_unified_observation,
)
from oilfield_energy.workflows.single_execution.contracts import (
    BoundedBusLoad,
    FirmGeneration,
    SingleForecastEnvelope,
)

from .contracts import (
    InitializationEvidence,
    InterfaceError,
    PccTargetEvidence,
    SnapshotAssessment,
    SubmittedSnapshot,
)
from .uncertain_assessment import assess_uncertain


def assess_snapshot(
    current: InitializationEvidence,
    snapshot: SubmittedSnapshot,
    target: PccTargetEvidence | None,
) -> tuple[SnapshotAssessment, tuple[InterfaceError, ...]]:
    """Keep measured values unchanged even if declared bounds or safety fail."""
    base = current.request.payload
    offset = (
        snapshot.snapshot_at - base.planning_inputs.effective_from
    ).total_seconds()
    step = int(offset // 60)
    evidence = SnapshotAssessment(context_id=current.domain.context_id, step=step)
    if (
        offset < 0
        or offset % 60
        or snapshot.snapshot_at >= current.valid_until
        or step >= len(current.domain.forecasts)
    ):
        return evidence, (
            InterfaceError(
                code="FORECAST_COVERAGE_INSUFFICIENT",
                message="Snapshot lies outside explicit active forecast/authority coverage",
            ),
        )
    if base.control_policy.observation_uncertainty:
        return assess_uncertain(current, snapshot, target, step)
    states = {x.resource_id: x for x in snapshot.device_states}
    storage = next(d for d in base.model_package.devices if d.kind == "storage")

    def number(value: float | None) -> float:
        assert value is not None  # Quality/identity validation precedes this mapping.
        return value

    energy = states[storage.resource_id].energy_mwh
    assert energy is not None
    state = TubeState(
        p_mw=tuple(
            number(states[r.resource_id].p_mw.value)
            for r in current.domain.devices.resources
        ),
        q_mvar=tuple(
            number(states[r.resource_id].q_mvar.value)
            for r in current.domain.devices.resources
        ),
        energy_mwh=number(energy.value),
    )
    observed = SingleForecastEnvelope(
        loads=tuple(
            BoundedBusLoad(
                bus=b.bus_id,
                p_mw=I(low=number(b.p_mw.value), high=number(b.p_mw.value)),
                q_mvar=I(low=number(b.q_mvar.value), high=number(b.q_mvar.value)),
            )
            for b in snapshot.bus_loads
        ),
        generation=tuple(
            FirmGeneration(
                resource_id=g.resource_id,
                minimum_available_mw=number(g.available_p_mw.value),
            )
            for g in snapshot.renewable_availability
        ),
    )
    evidence = evidence.model_copy(
        update={"observed_state": state, "observed_inputs": observed}
    )
    errors: list[InterfaceError] = []
    for code, check in (
        (
            "OBSERVATION_OUTSIDE_ENVELOPE",
            lambda: validate_unified_observation(current.domain, step, observed),
        ),
        (
            "STATE_OUTSIDE_CERTIFICATE",
            lambda: validate_device_box(
                device_model_at(current.domain, step), point_box(state)
            ),
        ),
    ):
        try:
            check()
        except (ValueError, ArithmeticError) as exc:
            errors.append(InterfaceError(code=code, message=str(exc)))
    # The conservative renewable device envelope is deliberately kept distinct
    # from nameplate/available power: exceeding it invalidates the existing proof.
    try:
        network = unified_network_box(current.domain, point_box(state), observed)
        evidence = evidence.model_copy(update={"network": network})
        if not network.certified:
            errors.append(
                InterfaceError(code="SNAPSHOT_SAFETY_UNPROVEN", message=network.reason)
            )
        else:
            assert network.pcc_p_mw is not None and network.pcc_q_mvar is not None
            for value, bound, tolerance in (
                (
                    snapshot.pcc.p_import_mw.value,
                    network.pcc_p_mw,
                    base.control_policy.pcc_model_tolerance_mw,
                ),
                (
                    snapshot.pcc.q_import_mvar.value,
                    network.pcc_q_mvar,
                    base.control_policy.pcc_model_tolerance_mvar,
                ),
            ):
                if not bound.low - tolerance <= number(value) <= bound.high + tolerance:
                    errors.append(
                        InterfaceError(
                            code="STATE_OUTSIDE_CERTIFICATE",
                            message="Measured PCC differs from modeled observed injections",
                        )
                    )
    except (ValueError, ArithmeticError) as exc:
        errors.append(InterfaceError(code="SNAPSHOT_SAFETY_UNPROVEN", message=str(exc)))
    if target and target.context_id == current.domain.context_id:
        tracking = target.plan.tracking
        index = step - target.plan.start_step
        if (
            tracking
            and base.control_policy.bounded_timing is None
            and snapshot.snapshot_at < target.guaranteed_until
            and 0 <= index < len(tracking.tubes)
        ):
            if not tube_contains(tracking.tubes[index], state):
                errors.append(
                    InterfaceError(
                        code="STATE_OUTSIDE_CERTIFICATE",
                        message="Observed state left the retained target tube; execution remains unconfirmed",
                    )
                )
        obligation = target.request.payload
        if (
            obligation.response_deadline_at
            <= snapshot.snapshot_at
            < target.guaranteed_until
        ):
            if (
                abs(
                    number(snapshot.pcc.p_import_mw.value)
                    - obligation.target_p_import_mw
                )
                > obligation.p_tolerance_mw
                or abs(
                    number(snapshot.pcc.q_import_mvar.value)
                    - obligation.target_q_import_mvar
                )
                > obligation.q_tolerance_mvar
            ):
                errors.append(
                    InterfaceError(
                        code="TARGET_TRACKING_VIOLATED",
                        message="Measured PCC is outside the accepted bands after the original response deadline",
                    )
                )
    return evidence, tuple(errors)

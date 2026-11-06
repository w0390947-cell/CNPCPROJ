"""Shared initial-state admission proofs; no persistence or device execution."""

import json
from collections.abc import Callable
from datetime import datetime
from fractions import Fraction as F
from hashlib import sha256

from pydantic import TypeAdapter

from oilfield_energy.modules.control.api import point_box, validate_device_box
from oilfield_energy.modules.power_flow.contracts import (
    PowerInterval as I,
)
from oilfield_energy.modules.power_flow.contracts import (
    RadialEnvelope,
)
from oilfield_energy.workflows.single_execution.api import (
    certify_unified_plan,
    certify_unified_terminal,
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
    InitializationFailure,
    InitializeRequest,
    PointMapping,
)
from .domain import build_domain
from .messages import canonical
from .observation_uncertainty import observe_bounded, validate_bounded_forecast
from .timing_certification import (
    certify_timed_continuation,
    midpoint,
    preparation_deadline,
)
from .validation import require, validate_snapshot


def _number(value: float | None) -> float:
    if value is None:
        raise InitializationFailure(
            "TELEMETRY_INVALID", "Required measurement is missing"
        )
    return value


def certify_conditions(
    request: InitializeRequest,
    network: RadialEnvelope,
    mapping: tuple[PointMapping, ...],
    clock: Callable[[], datetime],
    elapsed: Callable[[], float],
    started: float,
    *,
    completed_energy_task: bool = False,
) -> InitializationEvidence:
    p = request.payload
    validate_snapshot(
        request,
        network.buses,
        network.buses[network.slack],
        mapping,
        clock(),
    )
    # Bind resolved model data, versions, mapping, tariffs, policy and clocks.
    material = (
        canonical(request)
        + json.dumps(p.model_package.network_model, sort_keys=True).encode()
        + TypeAdapter(tuple[PointMapping, ...]).dump_json(mapping)
    )
    if completed_energy_task:
        material += b"|completed-energy-task-v1"
    context = sha256(material).hexdigest()
    domain, initial = build_domain(
        request, network, context, completed_energy_task=completed_energy_task
    )
    bounded = (
        observe_bounded(
            p.initial_snapshot,
            p.model_package,
            p.control_policy,
            tuple(r.resource_id for r in domain.devices.resources),
        )
        if p.control_policy.observation_uncertainty
        else None
    )
    initial_box = bounded.state_box if bounded else point_box(initial)
    if bounded:
        initial = midpoint(initial_box)
    try:
        validate_device_box(device_model_at(domain, 0), initial_box)
    except ValueError as exc:
        raise InitializationFailure("INITIAL_SAFETY_UNPROVEN", str(exc)) from exc
    s = p.initial_snapshot
    observed = SingleForecastEnvelope(
        loads=tuple(
            BoundedBusLoad(
                bus=b.bus_id,
                p_mw=I(low=_number(b.p_mw.value), high=_number(b.p_mw.value)),
                q_mvar=I(low=_number(b.q_mvar.value), high=_number(b.q_mvar.value)),
            )
            for b in s.bus_loads
        ),
        generation=tuple(
            FirmGeneration(
                resource_id=g.resource_id,
                minimum_available_mw=_number(g.available_p_mw.value),
            )
            for g in s.renewable_availability
        ),
    )
    if bounded:
        observed = bounded.inputs
    try:
        if bounded:
            validate_bounded_forecast(domain.forecasts[0], observed)
        else:
            validate_unified_observation(domain, 0, observed)
    except ValueError as exc:
        raise InitializationFailure("OBSERVATION_OUTSIDE_ENVELOPE", str(exc)) from exc
    initial_ac = unified_network_box(domain, initial_box, observed)
    require(initial_ac.certified, "INITIAL_SAFETY_UNPROVEN", initial_ac.reason)
    assert initial_ac.pcc_p_mw is not None and initial_ac.pcc_q_mvar is not None
    for value, bound, tolerance in (
        (
            s.pcc.p_import_mw.value,
            initial_ac.pcc_p_mw,
            p.control_policy.pcc_model_tolerance_mw,
        ),
        (
            s.pcc.q_import_mvar.value,
            initial_ac.pcc_q_mvar,
            p.control_policy.pcc_model_tolerance_mvar,
        ),
    ):
        require(
            bounded is not None
            or value is not None
            and bound.low - tolerance <= value <= bound.high + tolerance,
            "STATE_OUTSIDE_CERTIFICATE",
            "Measured PCC differs from modeled initial injections",
        )
    if bounded:
        for sample, bound, tolerance in (
            (
                s.pcc.p_import_mw,
                initial_ac.pcc_p_mw,
                p.control_policy.pcc_model_tolerance_mw,
            ),
            (
                s.pcc.q_import_mvar,
                initial_ac.pcc_q_mvar,
                p.control_policy.pcc_model_tolerance_mvar,
            ),
        ):
            r = bounded.aligned_ranges[sample.source_ref]
            require(
                F(r.high) >= F(bound.low) - F(tolerance)
                and F(r.low) <= F(bound.high) + F(tolerance),
                "OBSERVATION_INCONSISTENT",
                "PCC and injection enclosures are disjoint",
            )
    terminal = certify_unified_terminal(domain)
    task = p.planning_inputs.energy_task
    try:
        proof = certify_unified_plan(
            domain,
            initial_box,
            0,
            require_task=bool(task and task.required and not completed_energy_task),
        )
    except ValueError as exc:
        raise InitializationFailure(
            "TASK_UNPROVEN" if task and task.required else "INITIAL_SAFETY_UNPROVEN",
            str(exc),
        ) from exc
    require(
        len(proof.commands) <= len(domain.forecasts) - 1,
        "FORECAST_COVERAGE_INSUFFICIENT",
        "Backup exceeds bounded field coverage",
    )
    require(
        elapsed() - started <= p.control_policy.compute_budget_seconds,
        "COMPUTE_BUDGET_EXCEEDED",
        "Initialization exceeded its admission budget; no ready session committed",
    )
    require(
        clock() < min(p.planning_inputs.valid_until, p.authority.valid_until),
        "TELEMETRY_INVALID",
        "Inputs or authority expired during certification",
    )
    validate_snapshot(
        request,
        network.buses,
        network.buses[network.slack],
        mapping,
        clock(),
    )
    evidence = InitializationEvidence(
        initial_box=initial_box if bounded else None,
        request=request,
        resolved_model_package=p.model_package,
        domain=domain,
        initial=initial,
        plan=proof,
        terminal_proof_digest=terminal,
        valid_until=min(p.planning_inputs.valid_until, p.authority.valid_until),
        completed_energy_task=completed_energy_task,
    )
    if p.control_policy.observation_uncertainty:
        evidence = evidence.model_copy(
            update={
                "valid_until": min(
                    evidence.valid_until,
                    p.control_policy.observation_uncertainty.valid_until,
                )
            }
        )
    if p.control_policy.bounded_timing:
        evidence = evidence.model_copy(
            update={"timed_proof": certify_timed_continuation(evidence)}
        )
        require(
            elapsed() - started <= p.control_policy.compute_budget_seconds,
            "COMPUTE_BUDGET_EXCEEDED",
            "Timed admission exceeded its calculation budget",
        )
        validate_snapshot(
            request, network.buses, network.buses[network.slack], mapping, clock()
        )
        require(
            clock() <= preparation_deadline(evidence),
            "EXECUTION_TIMING_UNSUPPORTED",
            "Timed admission completed too late for its first application window",
        )
    return evidence

"""Economic proposals, certified continuation, and exactly one physical executor.

The finite-horizon forecast envelope persists at its last sample. A terminal
command is proved safe for every remaining sample and the persistent tail.
Optimization success is never a premise of the safety induction.
"""

from hashlib import sha256

from oilfield_energy.modules.control.api import (
    backup_command_candidates,
    certify_device_transition,
    point_box,
    validate_device_box,
)
from oilfield_energy.modules.control.contracts import (
    TubeBox,
    TubeCommand,
    TubeState,
)

from .contracts_unified import (
    SingleForecastEnvelope,
    UnifiedDomain,
    UnifiedPlanCertificate,
)
from .network import (
    device_model_at as device_model_at,
)
from .network import (
    forecast_at as forecast_at,
)
from .network import (
    unified_network_box as unified_network_box,
)
from .tasks import TaskCertificateUnavailable, complete_energy_task


def _terminal(domain: UnifiedDomain, box: TubeBox) -> bool:
    return all(
        x.low == x.high == u
        for x, u in zip(
            (*box.p_mw, *box.q_mvar),
            (*domain.terminal_command.p_mw, *domain.terminal_command.q_mvar),
            strict=True,
        )
    )


def certify_unified_terminal(domain: UnifiedDomain) -> str:
    command = domain.terminal_command
    if any(p != 0 for p in command.p_mw):
        raise ValueError(
            "the certified terminal policy requires zero active generation and PCS power"
        )
    box = point_box(
        TubeState(
            p_mw=command.p_mw,
            q_mvar=command.q_mvar,
            energy_mwh=domain.devices.energy_mwh.low,
        )
    )
    box = box.model_copy(update={"energy_mwh": domain.devices.energy_mwh})
    digest = sha256()
    seen: set[str] = set()
    for k, forecast in enumerate(domain.forecasts):
        identity = forecast.model_dump_json()
        if identity in seen:
            continue
        seen.add(identity)
        transition = certify_device_transition(device_model_at(domain, k), box, command)
        if transition.endpoint != box:
            raise ValueError("terminal device set is not invariant")
        network = unified_network_box(domain, box, forecast, reusable=True)
        if not network.certified:
            raise ValueError("terminal AC continuation unavailable: " + network.reason)
        digest.update(network.model_dump_json().encode())
    return digest.hexdigest()


def certify_unified_plan(
    domain: UnifiedDomain,
    initial: TubeBox,
    start: int,
    proposal: tuple[TubeCommand, ...] = (),
    *,
    require_task: bool = True,
) -> UnifiedPlanCertificate:
    """Prefer PCS-independent recovery, retry the full scaled tail if needed."""
    try:
        return _certify_plan(domain, initial, start, proposal, require_task, True)
    except ValueError as independent_error:
        try:
            return _certify_plan(domain, initial, start, proposal, require_task, False)
        except ValueError as scaled_error:
            # Preserve the hard-task failure classification used at startup.
            if isinstance(independent_error, TaskCertificateUnavailable):
                raise independent_error from scaled_error
            raise scaled_error from independent_error


def _certify_plan(
    domain: UnifiedDomain,
    initial: TubeBox,
    start: int,
    proposal: tuple[TubeCommand, ...],
    require_task: bool,
    independent_pcs: bool,
) -> UnifiedPlanCertificate:
    box = initial
    commands: list[TubeCommand] = []
    digest = sha256()
    for offset in range(len(proposal) + domain.research.backup_maximum_steps + 1):
        if offset >= len(proposal) and _terminal(domain, box):
            plan = UnifiedPlanCertificate(
                start_step=start,
                initial=initial,
                commands=tuple(commands),
                terminal=box,
                network_proof_digest=digest.hexdigest(),
            )
            try:
                return complete_energy_task(domain, plan)
            except TaskCertificateUnavailable as exc:
                if require_task:
                    raise
                return plan.model_copy(update={"task_reason": str(exc)})
        model = device_model_at(domain, start + offset)
        # Smaller certified backup movements recover paths whose independent
        # P/Q box would otherwise lose too much correlation. No failed trial
        # advances time, state, energy, or the retained certificate.
        fractions = (
            (1.0,) if offset < len(proposal) else (1.0, 0.5, 0.25, 0.125, 0.0625)
        )
        failure = "backup path unavailable"
        accepted = None
        for fraction in fractions:
            try:
                candidates = (
                    (proposal[offset],)
                    if offset < len(proposal)
                    else backup_command_candidates(
                        model,
                        box,
                        domain.terminal_command,
                        movement_fraction=fraction,
                        next_model=device_model_at(domain, start + offset + 1),
                        independent_pcs=independent_pcs,
                    )
                )
            except ValueError as exc:
                failure = str(exc)
                continue
            for command in candidates:
                try:
                    transition = certify_device_transition(model, box, command)
                    validate_device_box(
                        device_model_at(domain, start + offset + 1), transition.endpoint
                    )
                    network = unified_network_box(
                        domain,
                        transition.path,
                        forecast_at(domain, start + offset),
                        reusable=True,
                    )
                    if not network.certified:
                        raise ValueError(
                            "backup path AC certificate unavailable: " + network.reason
                        )
                except ValueError as exc:
                    failure = str(exc)
                else:
                    accepted = command, transition, network
                    break
            if accepted is not None:
                break
        if accepted is None:
            raise ValueError(failure)
        command, transition, network = accepted
        digest.update(network.model_dump_json().encode())
        commands.append(command)
        box = transition.endpoint
    raise ValueError(
        "backup does not enter its invariant terminal set within the declared horizon"
    )


def validate_unified_observation(
    domain: UnifiedDomain, step: int, actual: SingleForecastEnvelope
) -> None:
    expected = forecast_at(domain, step)
    loads = {x.bus: x for x in actual.loads}
    generation = {x.resource_id: x.minimum_available_mw for x in actual.generation}
    if len(loads) != len(actual.loads) or loads.keys() != {
        x.bus for x in expected.loads
    }:
        raise ValueError("load observation identities differ")
    for bound in expected.loads:
        value = loads[bound.bus]
        if (
            value.p_mw.low != value.p_mw.high
            or value.q_mvar.low != value.q_mvar.high
            or not bound.p_mw.contains(value.p_mw.low)
            or not bound.q_mvar.contains(value.q_mvar.low)
        ):
            raise ValueError("load observation left the declared forecast envelope")
    if (
        len(generation) != len(actual.generation)
        or generation.keys() != {x.resource_id for x in expected.generation}
        or any(
            generation[x.resource_id] < x.minimum_available_mw
            for x in expected.generation
        )
    ):
        raise ValueError("renewable availability left the declared forecast envelope")
